import streamlit as st
from streamlit_gsheets import GSheetsConnection
import pandas as pd
import requests
import math
import json
import time
import urllib.parse
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials

# --- APP UI SETUP ---
st.set_page_config(page_title="Lumber Hub: Universal Trading Desk", layout="wide")

# --- PLATFORM SWITCHER ---
st.sidebar.header("🚀 Workspace Selector")
app_mode = st.sidebar.radio("Active Platform:", ["🌲 Lumber Trading", "🪵 Molding & Millwork"])

# --- CONNECTIONS ---
conn = st.connection("gsheets", type=GSheetsConnection)

def get_gspread_client():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_dict = st.secrets["connections"]["gsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    return gspread.authorize(creds)

# --- SESSION STATE LOCKING ---
if "master_data" not in st.session_state: st.session_state.master_data = None
if "spec_data" not in st.session_state: st.session_state.spec_data = None
if "molding_data" not in st.session_state: st.session_state.molding_data = None
if "crm_data" not in st.session_state: st.session_state.crm_data = None
if "profiles_list" not in st.session_state: st.session_state.profiles_list = None

# --- PASSIVE INITIALIZATION ---
if st.session_state.profiles_list is None:
    try:
        df_p = conn.read(worksheet="Profiles", ttl=600)
        st.session_state.profiles_list = df_p
    except:
        st.error("Google API Limit Reached. Please wait 60 seconds and refresh.")
        st.stop()

df_profiles = st.session_state.profiles_list
profile_names = df_profiles["profile_name"].unique().tolist() if not df_profiles.empty else ["Default"]

# --- SIDEBAR: PROFILE & SECURITY ---
st.sidebar.header("📁 Profile Manager")
selected_profile = st.sidebar.selectbox("Select Active Profile", profile_names)
current_profile = selected_profile.strip()

user_pin = st.sidebar.text_input("Enter 4-Digit PIN", type="password")

# --- CONFIG DECODING ---
saved_config = {}
is_locked = True
if current_profile in df_profiles["profile_name"].values:
    config_str = df_profiles[df_profiles["profile_name"] == current_profile]["config_json"].values[0]
    try:
        saved_config = json.loads(config_str)
        correct_pin = str(saved_config.get("pin", ""))
        if not correct_pin or user_pin == correct_pin: is_locked = False
    except: is_locked = False

if is_locked:
    st.warning("🔒 Enter PIN to unlock Cloud Data.")
    st.stop()

# --- DATA HYDRATION ---
if st.session_state.master_data is None: st.session_state.master_data = saved_config.get("master_data", [])
if st.session_state.spec_data is None: st.session_state.spec_data = saved_config.get("spec_data", [])
if st.session_state.molding_data is None: st.session_state.molding_data = saved_config.get("molding_data", [])

# --- FREIGHT SETTINGS ---
st.sidebar.header("1. Freight & Logic")
states_input, rates_input = [], []
d_states = saved_config.get("states", [""]*6) 
d_rates = saved_config.get("rates", [0.00]*6) 
for i in range(6):
    c1, c2 = st.sidebar.columns([1, 2])
    s = c1.text_input(f"St {i+1}", d_states[i], key=f"s{i}").upper().strip()
    r = c2.number_input(f"Rate {i+1}", value=float(d_rates[i]), key=f"r{i}")
    states_input.append(s); rates_input.append(r)

rate_map = {k: v for k, v in zip(states_input, rates_input) if k}
sh_threshold = st.sidebar.number_input("Short Haul Limit (Miles)", value=float(saved_config.get("sh_threshold", 200)))
sh_floor = st.sidebar.number_input("Short Haul Floor ($)", value=float(saved_config.get("sh_floor", 700)))
uni_div = st.sidebar.number_input("Lumber Std Divisor", value=float(saved_config.get("uni_div", 23.0)))
msr_div = st.sidebar.number_input("Lumber MSR Divisor", value=float(saved_config.get("msr_div", 25.0)))
molding_div = st.sidebar.number_input("Molding LF per Truck", value=float(saved_config.get("molding_div", 45000.0)))
round_val = st.sidebar.selectbox("Lumber Rounding", [1, 5, 10, 0], index=1)
cities_list_raw = st.sidebar.text_area("Master Cities", value=saved_config.get("cities_list", ""), height=100)
active_cities = sorted(list(set([c.strip() for c in cities_list_raw.split("\n") if c.strip()])))

# --- MILEAGE ENGINE ---
@st.cache_data(ttl=3600)
def fetch_mileage_cache(): return conn.read(worksheet="Mileage")

def get_miles(origin, destination):
    if not origin or not destination: return None
    lane_key = f"{origin.strip().upper()} to {destination.strip().upper()}"
    m_df = fetch_mileage_cache()
    if not m_df.empty and lane_key in m_df['lane_key'].values:
        return float(m_df[m_df['lane_key'] == lane_key]['miles'].values[0])
    time.sleep(1.2)
    try:
        res_a = requests.get(f"https://nominatim.openstreetmap.org/search?q={origin.strip()}&format=json&limit=1").json()
        res_b = requests.get(f"https://nominatim.openstreetmap.org/search?q={destination.strip()}&format=json&limit=1").json()
        r_url = f"http://router.project-osrm.org/route/v1/driving/{res_a[0]['lon']},{res_a[0]['lat']};{res_b[0]['lon']},{res_b[0]['lat']}?overview=false"
        miles = round(requests.get(r_url).json()['routes'][0]['distance'] * 0.000621371, 2)
        gc = get_gspread_client()
        ws = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"]).worksheet("Mileage")
        ws.append_row([lane_key, miles]); st.cache_data.clear(); return miles
    except: return None

# --- CALC ENGINE (SHARED) ---
def run_calculation(city, df_combined, r_map, r_rule, is_molding=False, for_outlook=False):
    if "Include" in df_combined.columns: df_combined = df_combined[df_combined["Include"] == True]
    price_col = 'Raw $/m' if is_molding else 'FOB Price'
    df_combined = df_combined[pd.to_numeric(df_combined[price_col], errors='coerce') > 0]
    sep = " --- " if for_outlook else "    "
    rows = []
    
    for _, r in df_combined.iterrows():
        prod, origin = str(r.get('Product' if not is_molding else 'Profile', '')), str(r.get('Origin', '')).upper()
        rate = next((v for k, v in r_map.items() if k in origin), None)
        if rate is None: continue
        miles = get_miles(origin, city)
        if miles:
            truck_cost = sh_floor if miles < sh_threshold else miles * rate
            if is_molding:
                base_ft = float(r['Raw $/m']) / 3.28084
                mfg_cost = base_ft * float(r['Rip Factor']) * float(r['Finish Factor'])
                freight_per_lf = truck_cost / molding_div
                raw_p = mfg_cost + freight_per_lf
                p_label = f"${raw_p:,.3f}"
            else:
                div = msr_div if "MSR" in prod.upper() else uni_div
                raw_p = float(r['FOB Price']) + (truck_cost / div)
                p = math.ceil(raw_p / r_rule) * r_rule if r_rule > 0 else round(raw_p, 2)
                p_label = f"${p:>8,.2f}"
            
            avail, ship = str(r.get('Availability', 'Prompt')), str(r.get('Ship Time', 'Prompt'))
            line = f"{prod[:28]:<28}{sep}{avail[:10]:<10}{sep}{ship[:10]:<10}{sep}{p_label}"
            rows.append(line)
    return f"Quote: {city.upper()}\n\n{'PRODUCT':<28}{sep}{'AVAIL':<10}{sep}{'SHIP':<10}{sep}{'DELIVERED'}\n{'='*70}\n" + "\n".join(rows) if rows else None

# ==========================================
# PLATFORM A: LUMBER TRADING
# ==========================================
if app_mode == "🌲 Lumber Trading":
    t_price, t_bulk, t_crm = st.tabs(["🌲 Pricing Engine", "📦 Bulk Market", "👥 CRM"])

    with t_price:
        st.header(f"Lumber Workspace: {current_profile}")
        def prep_lumber_df(data, n):
            df = pd.DataFrame(data) if data else pd.DataFrame({"Include":[True]*n, "Product":[""]*n, "FOB Price":[0.0]*n, "Origin":[""]*n, "Stock":["High"]*n, "Availability":["Prompt"]*n, "Ship Time":["Prompt"]*n})
            if "Include" not in df.columns: df.insert(0, "Include", True)
            return df
        c1, c2 = st.columns(2)
        with c1: df_m_ui = st.data_editor(prep_lumber_df(st.session_state.master_data, 15), use_container_width=True, key="m_edit")
        with c2: df_s_ui = st.data_editor(prep_lumber_df(st.session_state.spec_data, 10), use_container_width=True, key="s_edit")
        
        city_pick = st.selectbox("Quick Price City", active_cities)
        if st.button("Calculate Lumber"):
            res = run_calculation(city_pick, pd.concat([df_m_ui, df_s_ui]), rate_map, round_val)
            if res: st.code(res)

    with t_bulk:
        if st.button("🚀 RUN FULL MARKET SHEET"):
            bulk_text = []
            df_full = pd.concat([df_m_ui, df_s_ui])
            for city in active_cities:
                q = run_calculation(city, df_full, rate_map, round_val)
                if q: bulk_text.append(q + "\n\n" + ("="*66) + "\n\n")
            st.code("".join(bulk_text))

# ==========================================
# PLATFORM B: MOLDING & MILLWORK
# ==========================================
elif app_mode == "🪵 Molding & Millwork":
    t_m_price, t_m_crm = st.tabs(["🪵 Millwork Engine", "👥 CRM"])

    with t_m_price:
        st.header(f"Millwork Workspace: {current_profile}")
        def prep_m_df(data):
            return pd.DataFrame(data) if data else pd.DataFrame({
                "Include": [True]*10, "Profile": [""]*10, "Raw $/m": [0.0]*10, "Origin": [""]*10,
                "Rip Factor": [1.15]*10, "Finish Factor": [0.333]*10, "Availability": ["Prompt"]*10, "Ship Time": ["Prompt"]*10
            })
        
        df_mold_ui = st.data_editor(prep_m_df(st.session_state.molding_data), use_container_width=True, num_rows="dynamic", key="mold_edit")
        
        m_city_pick = st.selectbox("Quick Price City", active_cities)
        if st.button("Calculate Delivered Molding"):
            res = run_calculation(m_city_pick, df_mold_ui, rate_map, round_val, is_molding=True)
            if res: st.code(res)

        st.markdown("---")
        
        

# ==========================================
# SHARED CRM (WORKS FOR BOTH)
# ==========================================
with st.expander("👥 CRM & OUTLOOK PREP"):
    if st.session_state.crm_data is None:
        try: st.session_state.crm_data = conn.read(worksheet="CRM", ttl=300).fillna("")
        except: st.session_state.crm_data = pd.DataFrame(columns=["profile_name", "Company Name", "Location", "Notes", "Buyer Email", "Last Quoted"])
    
    prof_crm = st.session_state.crm_data[st.session_state.crm_data['profile_name'] == current_profile]
    c_dir, c_prep = st.columns([2, 1])
    
    with c_prep:
        blurb = st.text_area("Daily Blurb", value=saved_config.get("daily_blurb", ""))
        cust = st.selectbox("Select Customer", ["-- Select --"] + list(prof_crm["Company Name"].unique()))
        if cust != "-- Select --":
            row = prof_crm[prof_crm["Company Name"] == cust].iloc[0]
            loc_list = [l.strip() for l in str(row['Location']).split(';') if l.strip()]
            if st.button("Generate Outlook Draft"):
                mq = []
                # Use whichever data is active
                active_inv = df_mold_ui if app_mode == "🪵 Molding & Millwork" else pd.concat([df_m_ui, df_s_ui])
                for city in loc_list:
                    q = run_calculation(city, active_inv, rate_map, round_val, is_molding=(app_mode=="🪵 Molding & Millwork"), for_outlook=True)
                    if q: mq.append(q)
                body = f"{blurb}\n\n" + "\n\n---\n\n".join(mq)
                mailto = f"mailto:{row['Buyer Email']}?subject=Quote&body={urllib.parse.quote(body)}"
                st.markdown(f'<a href="{mailto}" target="_blank"><div style="background-color:#0078d4;color:white;padding:15px;text-align:center;border-radius:8px;font-weight:bold;">OPEN IN OUTLOOK</div></a>', unsafe_allow_html=True)

    with c_dir:
        edit_crm = st.data_editor(prof_crm, use_container_width=True, num_rows="dynamic", key="crm_edit", column_order=("Company Name", "Location", "Last Quoted", "Notes", "Buyer Email"))
        if st.button("💾 SAVE CRM"):
            gc = get_gspread_client(); ws = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"]).worksheet("CRM")
            edit_crm['profile_name'] = current_profile
            final_crm = pd.concat([st.session_state.crm_data[st.session_state.crm_data['profile_name'] != current_profile], edit_crm], ignore_index=True).astype(str)
            ws.clear(); ws.update([final_crm.columns.tolist()] + final_crm.values.tolist())
            st.session_state.crm_data = final_crm; st.success("CRM Saved!"); time.sleep(1); st.rerun()

# --- UNIVERSAL SAVE PROFILE ---
st.sidebar.markdown("---")
if st.sidebar.button("☁️ SAVE ALL PROFILE DATA"):
    gc = get_gspread_client(); ws = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"]).worksheet("Profiles")
    
    # Bundle current state
    new_config = saved_config.copy()
    new_config.update({
        "pin": user_pin, "states": states_input, "rates": rates_input, 
        "sh_threshold": sh_threshold, "sh_floor": sh_floor, "uni_div": uni_div, "msr_div": msr_div, 
        "molding_div": molding_div, "round_to": round_val, "cities_list": cities_list_raw, "daily_blurb": blurb,
        "master_data": df_m_ui.to_dict('records') if app_mode == "🌲 Lumber Trading" else saved_config.get("master_data", []),
        "spec_data": df_s_ui.to_dict('records') if app_mode == "🌲 Lumber Trading" else saved_config.get("spec_data", []),
        "molding_data": df_mold_ui.to_dict('records') if app_mode == "🪵 Molding & Millwork" else saved_config.get("molding_data", [])
    })
    
    data = ws.get_all_records()
    row_idx = next((i + 2 for i, r in enumerate(data) if r['profile_name'] == current_profile), -1)
    if row_idx != -1: ws.update_cell(row_idx, 2, json.dumps(new_config))
    else: ws.append_row([current_profile, json.dumps(new_config)])
    st.session_state.profiles_list = None; st.sidebar.success("Saved!"); time.sleep(1); st.rerun()