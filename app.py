import streamlit as st
from streamlit_gsheets import GSheetsConnection
import pandas as pd
import requests
import math
import json
import time
import urllib.parse

# --- APP UI SETUP ---
st.set_page_config(page_title="Lumber Hub Cloud", layout="wide")

# --- GOOGLE SHEETS CONNECTION ---
conn = st.connection("gsheets", type=GSheetsConnection)

# --- PROFILE LOGIC ---
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
new_profile = st.sidebar.text_input("OR Create New (Blank)")
current_profile = new_profile if new_profile else selected_profile

# Load Config Data
saved_config = {}
if current_profile in df_profiles["profile_name"].values:
    saved_config = json.loads(df_profiles[df_profiles["profile_name"] == current_profile]["config_json"].values[0])

# --- SIDEBAR: 1. PRICING RATES (6 SLOTS) ---
st.sidebar.markdown("---")
st.sidebar.header("1. Pricing Rates")
states_input, rates_input = [], []
d_states = saved_config.get("states", ["", "", "", "", "", ""]) 
d_rates = saved_config.get("rates", [0.00] * 6) 

for i in range(6):
    c1, c2 = st.sidebar.columns([1, 2])
    s = c1.text_input(f"St {i+1}", d_states[i] if i < len(d_states) else "", key=f"s{i}").upper().strip()
    r = c2.number_input(f"Rate", value=float(d_rates[i]) if i < len(d_rates) else 0.00, key=f"r{i}")
    states_input.append(s); rates_input.append(r)

rate_map = {k: v for k, v in zip(states_input, rates_input) if k}

sh_threshold = st.sidebar.number_input("Short Haul Limit", value=float(saved_config.get("sh_threshold", 200)))
sh_floor = st.sidebar.number_input("Short Haul Floor ($)", value=float(saved_config.get("sh_floor", 700)))
uni_div = st.sidebar.number_input("Std Divisor", value=float(saved_config.get("uni_div", 23.0)))
msr_div = st.sidebar.number_input("MSR Divisor", value=float(saved_config.get("msr_div", 25.0)))
round_val = st.sidebar.selectbox("Rounding", [1, 5, 10, 0], index=1)

# --- SIDEBAR: 2. DESTINATION CITIES ---
st.sidebar.markdown("---")
st.sidebar.header("2. Cities")
cities_list = st.sidebar.text_area("City List (One per line)", value=saved_config.get("cities_list", ""), height=150)
active_cities = [c.strip() for c in cities_list.split("\n") if c.strip()]
target_city = st.sidebar.selectbox("Target for Quote", active_cities) if active_cities else None

# --- CALCULATION ENGINE ---
def get_miles(origin, destination):
    if not origin or not destination: return None
    time.sleep(1.1) # Compliance with OpenStreetMap
    try:
        headers = {'User-Agent': 'lumber_hub_cloud_v1'}
        res_a = requests.get(f"https://nominatim.openstreetmap.org/search?q={origin.strip()}&format=json&limit=1", headers=headers).json()
        res_b = requests.get(f"https://nominatim.openstreetmap.org/search?q={destination.strip()}&format=json&limit=1", headers=headers).json()
        c_a = (res_a[0]['lon'], res_a[0]['lat']); c_b = (res_b[0]['lon'], res_b[0]['lat'])
        r_url = f"http://router.project-osrm.org/route/v1/driving/{c_a[0]},{c_a[1]};{c_b[0]},{c_b[1]}?overview=false"
        return requests.get(r_url).json()['routes'][0]['distance'] * 0.000621371
    except: return None

def run_calculation(city, df_m, df_s, r_map, r_rule, inc_m, inc_s):
    combined_list = []
    if inc_m: combined_list.append(df_m)
    if inc_s: combined_list.append(df_s)
    if not combined_list: return None
    combined = pd.concat(combined_list)
    combined = combined[pd.to_numeric(combined['FOB Price'], errors='coerce') > 0]
    rows = []
    for _, r in combined.iterrows():
        prod, origin = str(r.get('Product', '')), str(r.get('Origin', '')).upper()
        avail, ship = str(r.get('Availability', 'Prompt')), str(r.get('Ship Time', 'Prompt'))
        if not prod or not origin: continue
        rate = next((v for k, v in r_map.items() if k in origin), None)
        if rate is None: continue
        miles = get_miles(origin, city)
        if miles:
            cost = sh_floor if miles < sh_threshold else miles * rate
            div = msr_div if "MSR" in prod.upper() else uni_div
            raw_p = float(r['FOB Price']) + (cost / div)
            p = math.ceil(raw_p / r_rule) * r_rule if r_rule > 0 else round(raw_p, 2)
            rows.append(f"{prod[:28]:<28} {avail[:10]:<10} {ship[:10]:<10} ${p:>7}")
    if rows:
        header = f"{'PRODUCT':<28} {'AVAIL':<10} {'SHIP':<10} {'PRICE':>8}"
        return f"QUOTE: {city.upper()}\n{header}\n{'-'*60}\n" + "\n".join(rows)
    return None

# --- SAVE ACTION ---
if st.sidebar.button("☁️ SAVE ALL TO CLOUD"):
    config_bundle = {
        "states": states_input, "rates": rates_input, "sh_threshold": sh_threshold, 
        "sh_floor": sh_floor, "uni_div": uni_div, "msr_div": msr_div,
        "round_to": round_val, "cities_list": cities_list,
        "master_data": st.session_state.m_edit.to_dict('records'),
        "spec_data": st.session_state.s_edit.to_dict('records')
    }
    new_row = pd.DataFrame([{"profile_name": current_profile, "config_json": json.dumps(config_bundle)}])
    updated_profiles = pd.concat([df_profiles[df_profiles["profile_name"] != current_profile], new_row])
    conn.update(worksheet="Profiles", data=updated_profiles)
    st.sidebar.success("Cloud Synced!")
    st.cache_data.clear()
    time.sleep(1); st.rerun()

# --- TABS ---
tab_pricing, tab_customers = st.tabs(["🌲 Pricing Engine", "👥 Cloud CRM"])

with tab_pricing:
    st.header(f"Workspace: {current_profile}")
    col_a, col_b = st.columns(2)
    with col_a:
        m_data = saved_config.get("master_data", [])
        df_master = st.data_editor(pd.DataFrame(m_data) if m_data else pd.DataFrame({"Product": [""]*15, "FOB Price": [0.0]*15, "Origin": [""]*15, "Availability": ["Prompt"]*15, "Ship Time": ["Prompt"]*15}), use_container_width=True, num_rows="dynamic", key="m_edit")
    with col_b:
        s_data = saved_config.get("spec_data", [])
        df_spec = st.data_editor(pd.DataFrame(s_data) if s_data else pd.DataFrame({"Product": [""]*10, "FOB Price": [0.0]*10, "Origin": [""]*10, "Availability": ["Prompt"]*10, "Ship Time": ["Prompt"]*10}), use_container_width=True, num_rows="dynamic", key="s_edit")

    st.markdown("---")
    inc_m = st.toggle("Include Standards", value=True)
    inc_s = st.toggle("Include Specialties", value=True)
    
    if st.button(f"Generate Quote for {target_city}", type="primary"):
        with st.spinner("Calculating Delivered Prices..."):
            res = run_calculation(target_city, df_master, df_spec, rate_map, round_val, inc_m, inc_s)
            if res: st.text_area("Output", value=res, height=300)

with tab_customers:
    st.header(f"CRM: {current_profile}")
    try:
        crm_all = conn.read(worksheet="CRM")
    except:
        crm_all = pd.DataFrame(columns=["profile_name", "Company Name", "Buyer Email", "Location", "Notes"])
    
    profile_crm = crm_all[crm_all['profile_name'] == current_profile]
    
    col1, col2 = st.columns([2, 1])
    with col2:
        st.subheader("📬 Quick Email")
        if not profile_crm.empty:
            cust = st.selectbox("Select Customer", ["-- Select --"] + list(profile_crm["Company Name"].unique()))
            if cust != "-- Select --":
                row = profile_crm[profile_crm["Company Name"] == cust].iloc[0]
                if st.button("🚀 PREPARE DRAFT"):
                    with st.spinner("Building Quote..."):
                        q = run_calculation(row['Location'], df_master, df_spec, rate_map, round_val, True, True)
                        mailto = f"mailto:{row['Buyer Email']}?subject={urllib.parse.quote(f'Quote - {cust}')}&body={urllib.parse.quote(q)}"
                        st.markdown(f'<a href="{mailto}" target="_blank" style="text-decoration:none;"><div style="background-color:#0078d4;color:white;padding:15px;text-align:center;border-radius:8px;font-weight:bold;">OPEN IN EMAIL CLIENT</div></a>', unsafe_allow_html=True)

    with col1:
        edited_crm = st.data_editor(profile_crm if not profile_crm.empty else pd.DataFrame(columns=["profile_name", "Company Name", "Buyer Email", "Location", "Notes"]), use_container_width=True, num_rows="dynamic", key="crm_edit")
        if st.button("💾 SAVE CRM"):
            edited_crm['profile_name'] = current_profile
            others = crm_all[crm_all['profile_name'] != current_profile]
            final_crm = pd.concat([others, edited_crm], ignore_index=True)
            conn.update(worksheet="CRM", data=final_crm)
            st.success("CRM Synced!")
            st.cache_data.clear()
            time.sleep(1); st.rerun()