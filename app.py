import streamlit as st
from streamlit_gsheets import GSheetsConnection
import pandas as pd
import requests
import math
import json
import time
import urllib.parse
import gspread
from google.oauth2.service_account import Credentials

# --- APP UI SETUP ---
st.set_page_config(page_title="Lumber Hub Cloud", layout="wide")

# --- CONNECTIONS ---
conn = st.connection("gsheets", type=GSheetsConnection)

def get_gspread_client():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds_dict = st.secrets["connections"]["gsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    return gspread.authorize(creds)

# --- DATA LOADING ---
@st.cache_data(ttl=2)
def get_all_profiles():
    try:
        df = conn.read(worksheet="Profiles")
        return df if not df.empty else pd.DataFrame(columns=["profile_name", "config_json"])
    except: return pd.DataFrame(columns=["profile_name", "config_json"])

df_profiles = get_all_profiles()
profile_list = df_profiles["profile_name"].unique().tolist() if not df_profiles.empty else ["Default"]

st.sidebar.header("📁 Cloud Profile Manager")
selected_profile = st.sidebar.selectbox("Select Active Profile", profile_list)
new_profile = st.sidebar.text_input("OR Create New")
current_profile = new_profile if new_profile else selected_profile

saved_config = {}
if current_profile in df_profiles["profile_name"].values:
    saved_config = json.loads(df_profiles[df_profiles["profile_name"] == current_profile]["config_json"].values[0])

# --- SIDEBAR INPUTS ---
st.sidebar.markdown("---")
states_input, rates_input = [], []
d_states = saved_config.get("states", ["", "", "", "", "", ""]) 
d_rates = saved_config.get("rates", [0.00] * 6) 

for i in range(6):
    c1, c2 = st.sidebar.columns([1, 2])
    s = c1.text_input(f"St {i+1}", d_states[i] if i < len(d_states) else "", key=f"s{i}").upper().strip()
    r = c2.number_input(f"Rate {i+1}", value=float(d_rates[i]) if i < len(d_rates) else 0.00, key=f"r{i}")
    states_input.append(s); rates_input.append(r)

rate_map = {k: v for k, v in zip(states_input, rates_input) if k}
sh_threshold = st.sidebar.number_input("Short Haul Limit", value=float(saved_config.get("sh_threshold", 200)))
sh_floor = st.sidebar.number_input("Short Haul Floor ($)", value=float(saved_config.get("sh_floor", 700)))
uni_div = st.sidebar.number_input("Std Divisor", value=float(saved_config.get("uni_div", 23.0)))
msr_div = st.sidebar.number_input("MSR Divisor", value=float(saved_config.get("msr_div", 25.0)))
round_val = st.sidebar.selectbox("Rounding", [1, 5, 10, 0], index=1)
cities_list = st.sidebar.text_area("City List", value=saved_config.get("cities_list", ""), height=100)
active_cities = [c.strip() for c in cities_list.split("\n") if c.strip()]
target_city = st.sidebar.selectbox("Target City", active_cities) if active_cities else None

# --- ENGINE ---
def get_miles(origin, destination):
    if not origin or not destination: return None
    time.sleep(1.1)
    try:
        res_a = requests.get(f"https://nominatim.openstreetmap.org/search?q={origin.strip()}&format=json&limit=1").json()
        res_b = requests.get(f"https://nominatim.openstreetmap.org/search?q={destination.strip()}&format=json&limit=1").json()
        r_url = f"http://router.project-osrm.org/route/v1/driving/{res_a[0]['lon']},{res_a[0]['lat']};{res_b[0]['lon']},{res_b[0]['lat']}?overview=false"
        return requests.get(r_url).json()['routes'][0]['distance'] * 0.000621371
    except: return None

def run_calculation(city, df_m, df_s, r_map, r_rule, inc_m, inc_s):
    combined = pd.concat(([df_m] if inc_m else []) + ([df_s] if inc_s else []))
    if combined.empty: return None
    combined = combined[pd.to_numeric(combined['FOB Price'], errors='coerce') > 0]
    rows = []
    for _, r in combined.iterrows():
        rate = next((v for k, v in r_map.items() if k in str(r.get('Origin', '')).upper()), None)
        miles = get_miles(str(r.get('Origin', '')), city)
        if rate is not None and miles:
            cost = max(sh_floor, miles * rate) if miles < sh_threshold else miles * rate
            div = msr_div if "MSR" in str(r.get('Product', '')).upper() else uni_div
            p = math.ceil((float(r['FOB Price']) + (cost/div)) / r_rule) * r_rule if r_rule > 0 else round(float(r['FOB Price']) + (cost/div), 2)
            rows.append(f"{str(r.get('Product', ''))[:28]:<28} {str(r.get('Availability', ''))[:10]:<10} ${p:>7}")
    return "\n".join(rows) if rows else "No matches."

# --- UI TABS ---
t1, t2 = st.tabs(["🌲 Pricing Engine", "👥 Cloud CRM"])

with t1:
    col_a, col_b = st.columns(2)
    m_data = saved_config.get("master_data", [])
    df_master = col_a.data_editor(pd.DataFrame(m_data) if m_data else pd.DataFrame({"Product":[""]*10, "FOB Price":[0.0]*10, "Origin":[""]*10, "Availability":["Prompt"]*10, "Ship Time":["Prompt"]*10}), key="m_edit", use_container_width=True, num_rows="dynamic")
    s_data = saved_config.get("spec_data", [])
    df_spec = col_b.data_editor(pd.DataFrame(s_data) if s_data else pd.DataFrame({"Product":[""]*5, "FOB Price":[0.0]*5, "Origin":[""]*5, "Availability":["Prompt"]*5, "Ship Time":["Prompt"]*5}), key="s_edit", use_container_width=True, num_rows="dynamic")
    
    if st.button("Generate Quote"):
        res = run_calculation(target_city, df_master, df_spec, rate_map, round_val, True, True)
        st.text_area("Result", res, height=200)

with t2:
    try: crm_all = conn.read(worksheet="CRM")
    except: crm_all = pd.DataFrame(columns=["profile_name", "Company Name", "Buyer Email", "Location", "Notes"])
    
    prof_crm = crm_all[crm_all['profile_name'] == current_profile]
    edited_crm = st.data_editor(prof_crm if not prof_crm.empty else pd.DataFrame(columns=["profile_name", "Company Name", "Buyer Email", "Location", "Notes"]), use_container_width=True, num_rows="dynamic", key="crm_edit")

    if st.button("💾 SAVE CRM"):
        gc = get_gspread_client()
        sh = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"])
        ws = sh.worksheet("CRM")
        # Overwrite CRM logic
        edited_crm['profile_name'] = current_profile
        final_crm = pd.concat([crm_all[crm_all['profile_name'] != current_profile], edited_crm], ignore_index=True)
        ws.clear()
        ws.update([final_crm.columns.values.tolist()] + final_crm.values.tolist())
        st.success("CRM Saved!")
        st.cache_data.clear()

# --- SAVE PROFILE ---
st.sidebar.markdown("---")
if st.sidebar.button("☁️ SAVE PROFILE DATA"):
    gc = get_gspread_client()
    sh = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"])
    ws = sh.worksheet("Profiles")
    
    config = {
        "states": states_input, "rates": rates_input, "sh_threshold": sh_threshold, "sh_floor": sh_floor,
        "uni_div": uni_div, "msr_div": msr_div, "round_to": round_val, "cities_list": cities_list,
        "master_data": df_master.to_dict('records'), "spec_data": df_spec.to_dict('records')
    }
    
    cell = None
    try: cell = ws.find(current_profile)
    except: pass
    
    if cell: ws.update_cell(cell.row, 2, json.dumps(config))
    else: ws.append_row([current_profile, json.dumps(config)])
    
    st.sidebar.success("Profile Saved!")
    st.cache_data.clear()
    time.sleep(1); st.rerun()