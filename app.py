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
st.set_page_config(page_title="Lumber Hub: State-Locked Master", layout="wide")

# --- CONNECTIONS ---
conn = st.connection("gsheets", type=GSheetsConnection)

def get_gspread_client():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_dict = st.secrets["connections"]["gsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    return gspread.authorize(creds)

# --- SESSION STATE INITIALIZATION ---
# This prevents the "Wipe to Default" bug by locking data into the browser session
if "master_data" not in st.session_state:
    st.session_state.master_data = None
if "spec_data" not in st.session_state:
    st.session_state.spec_data = None
if "crm_data" not in st.session_state:
    st.session_state.crm_data = None
if "active_profile_name" not in st.session_state:
    st.session_state.active_profile_name = ""

# --- DATA LOADING ---
@st.cache_data(ttl=600)
def fetch_raw_profiles():
    return conn.read(worksheet="Profiles")

df_profiles = fetch_raw_profiles()
profile_list = df_profiles["profile_name"].unique().tolist() if not df_profiles.empty else ["Default"]

# --- SIDEBAR: PROFILE & SECURITY ---
st.sidebar.header("📁 Cloud Profile Manager")
selected_profile = st.sidebar.selectbox("Select Active Profile", profile_list)
current_profile = selected_profile.strip()

# If user switches profiles, force a state reset to load new data
if st.session_state.active_profile_name != current_profile:
    st.session_state.master_data = None
    st.session_state.spec_data = None
    st.session_state.active_profile_name = current_profile

user_pin = st.sidebar.text_input("Enter 4-Digit PIN", type="password")

# --- CONFIG LOADING ---
saved_config = {}
is_locked = True

if current_profile in df_profiles["profile_name"].values:
    config_str = df_profiles[df_profiles["profile_name"] == current_profile]["config_json"].values[0]
    saved_config = json.loads(config_str)
    correct_pin = str(saved_config.get("pin", ""))
    if not correct_pin or user_pin == correct_pin:
        is_locked = False

if is_locked:
    st.error("🔒 Profile Locked. Enter correct PIN in sidebar.")
    st.stop()

# Load specific profile data into state if empty
if st.session_state.master_data is None:
    st.session_state.master_data = saved_config.get("master_data", [])
if st.session_state.spec_data is None:
    st.session_state.spec_data = saved_config.get("spec_data", [])

# --- SETTINGS ---
st.sidebar.header("1. Freight Rates")
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
round_val = st.sidebar.selectbox("Rounding", [1, 5, 10, 0], index=1)

st.sidebar.header("2. Engine Cities")
cities_list_raw = st.sidebar.text_area("Master City List", value=saved_config.get("cities_list", ""), height=120)
active_cities = sorted(list(set([c.strip() for c in cities_list_raw.split("\n") if c.strip()])))

# --- MILEAGE ENGINE ---
@st.cache_data(ttl=3600)
def get_cached_mileage():
    return conn.read(worksheet="Mileage")

def get_miles(origin, destination):
    if not origin or not destination: return None
    lane_key = f"{origin.strip().upper()} to {destination.strip().upper()}"
    m_df = get_cached_mileage()
    if not m_df.empty and lane_key in m_df['lane_key'].values:
        return float(m_df[m_df['lane_key'] == lane_key]['miles'].values[0])
    
    time.sleep(1.2)
    try:
        headers = {'User-Agent': 'lumber_hub_v19'}
        res_a = requests.get(f"https://nominatim.openstreetmap.org/search?q={origin.strip()}&format=json&limit=1", headers=headers).json()
        res_b = requests.get(f"https://nominatim.openstreetmap.org/search?q={destination.strip()}&format=json&limit=1", headers=headers).json()
        r_url = f"http://router.project-osrm.org/route/v1/driving/{res_a[0]['lon']},{res_a[0]['lat']};{res_b[0]['lon']},{res_b[0]['lat']}?overview=false"
        miles = round(requests.get(r_url).json()['routes'][0]['distance'] * 0.000621371, 2)
        gc = get_gspread_client()
        sh = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"])
        ws = sh.worksheet("Mileage")
        ws.append_row([lane_key, miles])
        st.cache_data.clear() 
        return miles
    except: return None

# --- CALC ENGINE ---
def run_calculation(city, df_combined, r_map, r_rule, for_outlook=False):
    if "Include" in df_combined.columns:
        df_combined = df_combined[df_combined["Include"] == True]
    df_combined = df_combined[pd.to_numeric(df_combined['FOB Price'], errors='coerce') > 0]
    sep = " --- " if for_outlook else "    "
    rows = []
    for _, r in df_combined.iterrows():
        prod, origin = str(r.get('Product', '')), str(r.get('Origin', '')).upper()
        avail, ship, stock = str(r.get('Availability', 'Prompt')), str(r.get('Ship Time', 'Prompt')), str(r.get('Stock', 'High'))
        stock_note = " (LTD)" if stock == "Low" else (" (SO)" if stock == "Out" else "")
        rate = next((v for k, v in r_map.items() if k in origin), None)
        if rate is None: continue
        miles = get_miles(origin, city)
        if miles:
            cost = sh_floor if miles < sh_threshold else miles * rate
            div = msr_div if "MSR" in prod.upper() else uni_div
            raw_p = float(r['FOB Price']) + (cost / div)
            p = math.ceil(raw_p / r_rule) * r_rule if r_rule > 0 else round(raw_p, 2)
            line = f"{prod[:28] + stock_note:<28}{sep}{avail[:10]:<10}{sep}{ship[:10]:<10}{sep}${p:>8,.2f}" if for_outlook else f"{prod[:30]:<30} {avail[:12]:<12} {ship[:12]:<12} ${p:>9,.2f}"
            rows.append(line)
    if not rows: return None
    header = f"{'PRODUCT':<28}{sep}{'AVAIL':<10}{sep}{'SHIP':<10}{sep}{'DELIVERED':>9}" if for_outlook else f"{'PRODUCT':<30} {'AVAIL':<12} {'SHIP':<12} {'DELIVERED':>10}"
    divider = "=" * len(header) if for_outlook else "-" * 66
    return f"Quote: {city.upper()}\n\n{header}\n{divider}\n" + "\n".join(rows)

# --- UI TABS ---
tab_pricing, tab_bulk, tab_customers = st.tabs(["🌲 Pricing Engine", "📦 Bulk Market", "👥 Cloud CRM"])

with tab_pricing:
    st.header(f"Workspace: {current_profile}")
    
    def prep_df(data, rows):
        df = pd.DataFrame(data) if data else pd.DataFrame({"Include": [True]*rows, "Product": [""]*rows, "FOB Price": [0.0]*rows, "Origin": [""]*rows, "Stock": ["High"]*rows, "Availability": ["Prompt"]*rows, "Ship Time": ["Prompt"]*rows})
        if "Include" not in df.columns: df.insert(0, "Include", True)
        return df

    c_m1, c_m2 = st.columns(2)
    with c_m1:
        st.subheader("Standard Master")
        df_master_ui = st.data_editor(prep_df(st.session_state.master_data, 15), use_container_width=True, num_rows="dynamic", key="m_edit")
    with c_m2:
        st.subheader("Specialty Master")
        df_spec_ui = st.data_editor(prep_df(st.session_state.spec_data, 10), use_container_width=True, num_rows="dynamic", key="s_edit")
    
    target_city = st.selectbox("Quick Single Target", active_cities) if active_cities else None
    if st.button("Generate Quote"):
        res = run_calculation(target_city, pd.concat([df_master_ui, df_spec_ui]), rate_map, round_val)
        if res: st.code(res, language="text")

with tab_bulk:
    st.header("Bulk Market Sheets")
    if st.button("🚀 RUN FULL MARKET SHEET"):
        bulk_output = []
        df_full = pd.concat([df_master_ui, df_spec_ui])
        for city in active_cities:
            q = run_calculation(city, df_full, rate_map, round_val)
            if q: bulk_output.append(q + "\n\n" + ("=" * 66) + "\n\n")
        if bulk_output: st.code("".join(bulk_output), language="text")

with tab_customers:
    st.header(f"Cloud CRM: {current_profile}")
    if st.session_state.crm_data is None:
        try:
            st.session_state.crm_data = conn.read(worksheet="CRM", ttl=300).fillna("")
        except:
            st.session_state.crm_data = pd.DataFrame(columns=["profile_name", "Company Name", "Location", "Notes", "Buyer Email", "Last Quoted"])
    
    df_crm = st.session_state.crm_data
    profile_crm = df_crm[df_crm['profile_name'] == current_profile]
    
    col_dir, col_prep = st.columns([2, 1])
    with col_prep:
        st.subheader("📬 Prepare Quote")
        daily_blurb = st.text_area("Daily Blurb", value=saved_config.get("daily_blurb", ""))
        if not profile_crm.empty:
            cust_name = st.selectbox("Select Customer", ["-- Select --"] + list(profile_crm["Company Name"].unique()))
            if cust_name != "-- Select --":
                c_row = profile_crm[profile_crm["Company Name"] == cust_name].iloc[0]
                loc_list = [l.strip() for l in str(c_row['Location']).split(';') if l.strip()]
                if st.button(f"Open Outlook Draft"):
                    ts = datetime.now().strftime("%m/%d %H:%M")
                    # Update local state immediately
                    st.session_state.crm_data.loc[(st.session_state.crm_data['profile_name'] == current_profile) & (st.session_state.crm_data['Company Name'] == cust_name), "Last Quoted"] = ts
                    multi_q = []
                    df_full = pd.concat([df_master_ui, df_spec_ui])
                    for city in loc_list:
                        q = run_calculation(city, df_full, rate_map, round_val, True)
                        if q: multi_q.append(q)
                    final_body = f"{daily_blurb}\n\n" + "\n\n---\n\n".join(multi_q)
                    mailto = f"mailto:{c_row['Buyer Email']}?subject=Quote&body={urllib.parse.quote(final_body)}"
                    st.markdown(f'<a href="{mailto}" target="_blank"><div style="background-color:#0078d4;color:white;padding:15px;text-align:center;border-radius:8px;font-weight:bold;">OPEN IN OUTLOOK</div></a>', unsafe_allow_html=True)

    with col_dir:
        edited_crm = st.data_editor(profile_crm, use_container_width=True, num_rows="dynamic", key="crm_edit_final", 
                        column_order=("Company Name", "Location", "Last Quoted", "Notes", "Buyer Email"),
                        column_config={"Location": st.column_config.TextColumn("Locations (Use ; to separate)"), "Last Quoted": st.column_config.TextColumn(disabled=True)})
        
        if st.button("💾 SAVE CRM & SYNC CITIES"):
            with st.spinner("Pushing to Cloud..."):
                gc = get_gspread_client()
                sh = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"])
                ws_crm = sh.worksheet("CRM")
                edited_crm['profile_name'] = current_profile
                # Merge logic
                final_crm = pd.concat([st.session_state.crm_data[st.session_state.crm_data['profile_name'] != current_profile], edited_crm], ignore_index=True).astype(str)
                ws_crm.clear()
                ws_crm.update([final_crm.columns.values.tolist()] + final_crm.values.tolist())
                
                # Update Session State so the change is instant
                st.session_state.crm_data = final_crm
                st.success("CRM Synced!")
                time.sleep(1)
                st.rerun()

# --- SIDEBAR: SAVE PROFILE ---
st.sidebar.markdown("---")
if st.sidebar.button("☁️ SAVE PROFILE"):
    with st.spinner("Saving..."):
        gc = get_gspread_client()
        sh = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"])
        ws_prof = sh.worksheet("Profiles")
        
        # Sync Master Data back to state
        st.session_state.master_data = df_master_ui.to_dict('records')
        st.session_state.spec_data = df_spec_ui.to_dict('records')
        
        config = {
            "pin": user_pin, "states": states_input, "rates": rates_input, 
            "sh_threshold": sh_threshold, "sh_floor": sh_floor, "uni_div": uni_div, 
            "msr_div": msr_div, "round_to": round_val, "cities_list": cities_list_raw, 
            "daily_blurb": daily_blurb, 
            "master_data": st.session_state.master_data, 
            "spec_data": st.session_state.spec_data
        }
        
        profiles_data = ws_prof.get_all_records()
        f_row = next((i + 2 for i, row in enumerate(profiles_data) if row['profile_name'] == current_profile), -1)
        if f_row != -1: ws_prof.update_cell(f_row, 2, json.dumps(config))
        else: ws_prof.append_row([current_profile, json.dumps(config)])
        
        st.sidebar.success("Profile Saved!")
        st.cache_data.clear() # Only clear cache for the next login, don't wipe session state
        time.sleep(1)
        st.rerun()