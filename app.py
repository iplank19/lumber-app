import streamlit as st
from streamlit_gsheets import GSheetsConnection
import pandas as pd
import requests
import math
import json
import time
import urllib.parse

# --- GOOGLE SHEETS CONNECTION ---
conn = st.connection("gsheets", type=GSheetsConnection)

# --- PROFILE LOGIC ---
@st.cache_data(ttl=5)
def get_all_profiles():
    try:
        df = conn.read(worksheet="Profiles")
        return df if not df.empty else pd.DataFrame(columns=["profile_name", "config_json"])
    except: return pd.DataFrame(columns=["profile_name", "config_json"])

df_profiles = get_all_profiles()
profile_list = df_profiles["profile_name"].unique().tolist() if not df_profiles.empty else ["Default"]

st.sidebar.header("📁 Cloud Profiles")
selected_profile = st.sidebar.selectbox("Select Profile", profile_list)
new_profile = st.sidebar.text_input("OR Create New")
current_profile = new_profile if new_profile else selected_profile

# Load Config
saved_config = {}
if current_profile in df_profiles["profile_name"].values:
    saved_config = json.loads(df_profiles[df_profiles["profile_name"] == current_profile]["config_json"].values[0])

# --- SIDEBAR: PRICING CONFIG ---
st.sidebar.markdown("---")
states_input, rates_input = [], []
d_states = saved_config.get("states", ["", "", "", "", "", ""]) 
d_rates = saved_config.get("rates", [0.00] * 6) 

for i in range(6):
    col1, col2 = st.sidebar.columns([1, 2])
    s = col1.text_input(f"St {i+1}", d_states[i], key=f"s{i}").upper()
    r = col2.number_input(f"Rate", value=float(d_rates[i]), key=f"r{i}")
    states_input.append(s); rates_input.append(r)

rate_map = {k: v for k, v in zip(states_input, rates_input) if k}
uni_div = st.sidebar.number_input("Std Divisor", value=float(saved_config.get("uni_div", 23.0)))
msr_div = st.sidebar.number_input("MSR Divisor", value=float(saved_config.get("msr_div", 25.0)))
round_val = st.sidebar.selectbox("Rounding", [1, 5, 10, 0], index=1)
cities_list = st.sidebar.text_area("City List", value=saved_config.get("cities_list", ""))

# --- SAVE TO CLOUD ---
if st.sidebar.button("☁️ SAVE ALL DATA"):
    config_bundle = {
        "states": states_input, "rates": rates_input, "uni_div": uni_div, "msr_div": msr_div,
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

# --- TABS & LOGIC ---
tab1, tab2 = st.tabs(["🌲 Pricing", "👥 CRM"])

with tab1:
    st.header(f"Workspace: {current_profile}")
    m_data = saved_config.get("master_data", [])
    df_master = st.data_editor(pd.DataFrame(m_data) if m_data else pd.DataFrame({"Product": [""]*10, "FOB Price": [0.0]*10, "Origin": [""]*10, "Availability": ["Prompt"]*10, "Ship Time": ["Prompt"]*10}), key="m_edit", use_container_width=True, num_rows="dynamic")
    # (Calculation engine would go here calling Nominatim API)
    st.info("Calculations will appear here after setting rates and origins.")

with tab2:
    st.header("Cloud CRM")
    # CRM logic would read from "CRM" worksheet using same update logic as Profiles
    st.write("Manage your buyers here.")