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
st.set_page_config(page_title="Lumber Hub: Factory Edition", layout="wide")

# --- PLATFORM SWITCHER ---
st.sidebar.header("🚀 Workspace Selector")
app_mode = st.sidebar.radio("Active Platform:", ["🌲 Lumber Trading", "🪵 Molding & Millwork"])

# --- SHARED CONNECTIONS ---
conn = st.connection("gsheets", type=GSheetsConnection)

def get_gspread_client():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_dict = st.secrets["connections"]["gsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    return gspread.authorize(creds)

# --- FACTOR SHEET DATA (From User Image) ---
FACTOR_SHEET = {
    "4-4/4": {"1/2": 2.16, "3/4": 2.93, "1": 3.71, "1.5": 5.47, "2": 6.64, "3": 10.16, "4": 13.28, "6": 19.53},
    "3-5/4": {"1/2": 2.39, "3/4": 3.28, "1": 4.12, "1.5": 6.08, "2": 7.38, "3": 11.28, "4": 14.75, "6": 21.70},
    "3-6/4": {"1/2": 2.87, "3/4": 3.91, "1": 4.95, "1.5": 7.29, "2": 8.85, "3": 13.54, "4": 17.71, "6": 26.04},
    "2-5/4": {"1/2": 3.58, "3/4": 4.88, "1": 6.19, "1.5": 9.11, "2": 11.07, "3": 16.93, "4": 22.14, "6": 32.55},
    "2-8/4": {"1/2": 5.08, "3/4": 5.86, "1": 7.42, "1.5": 10.94, "2": 13.28, "3": 20.31, "4": 26.56, "6": 39.06}
}

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
        st.error("Google API Limit Reached. Please wait 60 seconds.")
        st.stop()

df_profiles = st.session_state.profiles_list
profile_names = df_profiles["profile_name"].unique().tolist() if not df_profiles.empty else ["Default"]

# --- SIDEBAR: PROFILE ---
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
        if not saved_config.get("pin") or user_pin == str(saved_config.get("pin")): is_locked = False
    except: is_locked = False

if is_locked:
    st.warning("🔒 Enter PIN to unlock.")
    st.stop()

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
sh_threshold = st.sidebar.number_input("Short Haul Limit", value=float(saved_config.get("sh_threshold", 200)))
sh_floor = st.sidebar.number_input("Short Haul Floor ($)", value=float(saved_config.get("sh_floor", 700)))
uni_div = st.sidebar.number_input("Std Divisor", value=float(saved_config.get("uni_div", 23.0)))
msr_div = st.sidebar.number_input("MSR Divisor", value=float(saved_config.get("msr_div", 25.0)))
molding_div = st.sidebar.number_input("Molding LF/Truck", value=float(saved_config.get("molding_div", 45000.0)))
round_val = st.sidebar.selectbox("Rounding", [1, 5, 10, 0], index=1)
cities_list_raw = st.sidebar.text_area("Master Cities", value=saved_config.get("cities_list", ""), height=100)
active_cities = sorted(list(set([c.strip() for c in cities_list_raw.split("\n") if c.strip()])))

# --- MILEAGE ENGINE ---
@st.cache_data(ttl=3600)
def fetch_mileage(): return conn.read(worksheet="Mileage")

def get_miles(origin, destination):
    if not origin or not destination: return None
    lane_key = f"{origin.strip().upper()} to {destination.strip().upper()}"
    m_df = fetch_mileage()
    if not m_df.empty and lane_key in m_df['lane_key'].values:
        return float(m_df[m_df['lane_key'] == lane_key]['miles'].values[0])
    time.sleep(1.2)
    try:
        res_a = requests.get(f"https://nominatim.openstreetmap.org/search?q={origin.strip()}&format=json&limit=1").json()
        res_b = requests.get(f"https://nominatim.openstreetmap.org/search?q={destination.strip()}&format=json&limit=1").json()
        r_url = f"http://router.project-osrm.org/route/v1/driving/{res_a[0]['lon']},{res_a[0]['lat']};{res_b[0]['lon']},{res_b[0]['lat']}?overview=false"
        miles = round(requests.get(r_url).json()['routes'][0]['distance'] * 0.000621371, 2)
        gc = get_gspread_client(); ws = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"]).worksheet("Mileage")
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
                # Factory Factor Sheet Logic
                base_ft = float(r['Raw $/m']) / 3.28084
                factor = float(r.get('Factor', 1.0))
                mfg_cost = base_ft * float(r.get('Rip Factor', 1.15)) * factor
                freight_per_lf = truck_cost / molding_div
                raw_p = mfg_cost + freight_per_lf
                p_label = f"${raw_p:,.3f}"
            else:
                raw_p = float(r['FOB Price']) + (truck_cost / (msr_div if "MSR" in prod.upper() else uni_div))
                p = math.ceil(raw_p / r_rule) * r_rule if r_rule > 0 else round(raw_p, 2)
                p_label = f"${p:>8,.2f}"
            
            avail, ship = str(r.get('Availability', 'Prompt')), str(r.get('Ship Time', 'Prompt'))
            line = f"{prod[:28]:<28}{sep}{avail[:10]:<10}{sep}{ship[:10]:<10}{sep}{p_label}"
            rows.append(line)
    return f"Quote: {city.upper()}\n\n{'PRODUCT':<28}{sep}{'AVAIL':<10}{sep}{'SHIP':<10}{sep}{'DELIVERED'}\n{'='*70}\n" + "\n".join(rows) if rows else None

# ==========================================
# PLATFORMS
# ==========================================
if app_mode == "🌲 Lumber Trading":
    t_price, t_bulk = st.tabs(["🌲 Pricing", "📦 Bulk"])
    if st.session_state.master_data is None: st.session_state.master_data = saved_config.get("master_data", [])
    if st.session_state.spec_data is None: st.session_state.spec_data = saved_config.get("spec_data", [])
    
    with t_price:
        def prep_l(d, n): 
            df = pd.DataFrame(d) if d else pd.DataFrame({"Include":[True]*n, "Product":[""]*n, "FOB Price":[0.0]*n, "Origin":[""]*n, "Stock":["High"]*n, "Availability":["Prompt"]*n, "Ship Time":["Prompt"]*n})
            if "Include" not in df.columns: df.insert(0, "Include", True)
            return df
        c1, c2 = st.columns(2)
        with c1: df_m_ui = st.data_editor(prep_l(st.session_state.master_data, 15), use_container_width=True, key="m_ed")
        with c2: df_s_ui = st.data_editor(prep_l(st.session_state.spec_data, 10), use_container_width=True, key="s_ed")
        if st.button("Calculate Lumber"):
            q = run_calculation(st.selectbox("City", active_cities, key="l_city"), pd.concat([df_m_ui, df_s_ui]), rate_map, round_val)
            if q: st.code(q)

elif app_mode == "🪵 Molding & Millwork":
    st.header("🪵 Factory Factor Engine")
    

    def prep_m(d):
        df = pd.DataFrame(d) if d else pd.DataFrame({
            "Include": [True]*10, "Profile": [""]*10, "Raw $/m": [0.0]*10, "Origin": [""]*10,
            "Thickness": ["4-4/4"]*10, "Width": ["3/4"]*10, "Rip Factor": [1.15]*10, "Factor": [1.0]*10
        })
        return df

    # Data Editor with Automatic Factor Lookups
    m_df = st.data_editor(prep_m(saved_config.get("molding_data")), use_container_width=True, num_rows="dynamic", key="mold_ed",
                          column_config={
                              "Thickness": st.column_config.SelectboxColumn("Thickness (Top Row)", options=list(FACTOR_SHEET.keys())),
                              "Width": st.column_config.SelectboxColumn("Finished Width", options=["1/2", "3/4", "1", "1.5", "2", "3", "4", "6"]),
                              "Factor": st.column_config.NumberColumn("Factor (Auto)", disabled=True)
                          })
    
    # Auto-apply the Factors from the Image
    for idx, row in m_df.iterrows():
        try: m_df.at[idx, 'Factor'] = FACTOR_SHEET[row['Thickness']][row['Width']]
        except: m_df.at[idx, 'Factor'] = 1.0

    m_city = st.selectbox("Target City", active_cities, key="m_city")
    if st.button("Calculate Delivered Molding"):
        q = run_calculation(m_city, m_df, rate_map, round_val, is_molding=True)
        if q: st.code(q)

# --- SHARED CRM & SAVE ---
with st.expander("👥 CRM & OUTLOOK"):
    if st.session_state.crm_data is None: st.session_state.crm_data = conn.read(worksheet="CRM", ttl=300).fillna("")
    p_crm = st.session_state.crm_data[st.session_state.crm_data['profile_name'] == current_profile]
    c_dir, c_prep = st.columns([2, 1])
    with c_prep:
        blurb = st.text_area("Daily Blurb", value=saved_config.get("daily_blurb", ""))
        cust = st.selectbox("Select Customer", ["-- Select --"] + list(p_crm["Company Name"].unique()))
        if cust != "-- Select --":
            r = p_crm[p_crm["Company Name"] == cust].iloc[0]
            if st.button("Open Outlook"):
                locs = [l.strip() for l in str(r['Location']).split(';') if l.strip()]
                inv = m_df if app_mode == "🪵 Molding & Millwork" else pd.concat([df_m_ui, df_s_ui])
                mq = [run_calculation(c, inv, rate_map, round_val, (app_mode=="🪵 Molding & Millwork"), True) for c in locs]
                body = f"{blurb}\n\n" + "\n\n---\n\n".join([x for x in mq if x])
                st.markdown(f'<a href="mailto:{r["Buyer Email"]}?subject=Quote&body={urllib.parse.quote(body)}" target="_blank">OPEN OUTLOOK</a>', unsafe_allow_html=True)

if st.sidebar.button("☁️ SAVE ALL"):
    gc = get_gspread_client(); ws = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"]).worksheet("Profiles")
    conf = saved_config.copy()
    conf.update({"pin":user_pin, "states":states_input, "rates":rates_input, "sh_threshold":sh_threshold, "sh_floor":sh_floor, "uni_div":uni_div, "msr_div":msr_div, "molding_div":molding_div, "cities_list":cities_list_raw, "daily_blurb":blurb})
    if app_mode == "🌲 Lumber Trading": 
        conf.update({"master_data": df_m_ui.to_dict('records'), "spec_data": df_s_ui.to_dict('records')})
    else: 
        conf.update({"molding_data": m_df.to_dict('records')})
    
    rows = ws.get_all_records()
    idx = next((i+2 for i, r in enumerate(rows) if r['profile_name'] == current_profile), -1)
    if idx != -1: ws.update_cell(idx, 2, json.dumps(conf))
    else: ws.append_row([current_profile, json.dumps(conf)])
    st.success("Saved!"); st.cache_data.clear(); st.rerun()