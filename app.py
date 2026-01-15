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
st.set_page_config(page_title="Lumber Hub: Text Master", layout="wide")

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
    except:
        return pd.DataFrame(columns=["profile_name", "config_json"])

df_profiles = get_all_profiles()
profile_list = df_profiles["profile_name"].unique().tolist() if not df_profiles.empty else ["Default"]

# --- SIDEBAR: PROFILE & SECURITY ---
st.sidebar.header("📁 Cloud Profile Manager")
selected_profile = st.sidebar.selectbox("Select Active Profile", profile_list)
new_profile = st.sidebar.text_input("OR Create New (Enter Name)")
current_profile = new_profile if new_profile else selected_profile

user_pin = st.sidebar.text_input("Enter 4-Digit PIN", type="password")

saved_config = {}
is_locked = True

if current_profile in df_profiles["profile_name"].values:
    config_str = df_profiles[df_profiles["profile_name"] == current_profile]["config_json"].values[0]
    saved_config = json.loads(config_str)
    correct_pin = str(saved_config.get("pin", ""))
    if not correct_pin or user_pin == correct_pin:
        is_locked = False
else:
    is_locked = False

if is_locked:
    st.error("🔒 Profile Locked. Enter PIN in sidebar.")
    st.stop()

# --- SIDEBAR: SETTINGS ---
st.sidebar.markdown("---")
st.sidebar.header("1. Freight Rates")
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

st.sidebar.header("2. Cities")
cities_list_raw = st.sidebar.text_area("City List", value=saved_config.get("cities_list", ""), height=120)
active_cities = [c.strip() for c in cities_list_raw.split("\n") if c.strip()]

# --- MILEAGE ENGINE ---
def get_miles(origin, destination):
    if not origin or not destination: return None
    lane_key = f"{origin.strip().upper()} to {destination.strip().upper()}"
    try:
        mileage_df = conn.read(worksheet="Mileage")
        if not mileage_df.empty and lane_key in mileage_df['lane_key'].values:
            return float(mileage_df[mileage_df['lane_key'] == lane_key]['miles'].values[0])
    except: pass
    time.sleep(1.1)
    try:
        headers = {'User-Agent': 'lumber_hub_text_v1'}
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

# --- TEXT GRID ENGINE ---
def run_calculation(city, df_master, df_spec, r_map, r_rule, inc_m, inc_s):
    combined_list = []
    if inc_m: combined_list.append(df_master)
    if inc_s: combined_list.append(df_spec)
    if not combined_list: return None
    combined = pd.concat(combined_list)
    combined = combined[pd.to_numeric(combined['FOB Price'], errors='coerce') > 0]
    
    # Building the Grid
    header = f"{'PRODUCT':<30} {'AVAIL':<12} {'SHIP':<12} {'DELIVERED':>10}"
    divider = "-" * 66
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
            # Alignment Logic: Left-aligned text columns, right-aligned price
            line = f"{prod[:30]:<30} {avail[:12]:<12} {ship[:12]:<12} ${p:>9,.2f}"
            rows.append(line)
    
    if not rows: return None
    return f"Quote: {city.upper()}\n\n{header}\n{divider}\n" + "\n".join(rows)

# --- UI TABS ---
tab_pricing, tab_bulk, tab_customers = st.tabs(["🌲 Pricing Engine", "📦 Bulk Market", "👥 Cloud CRM"])

with tab_pricing:
    st.header(f"Workspace: {current_profile}")
    col_a, col_b = st.columns(2)
    with col_a:
        m_data = saved_config.get("master_data", [])
        df_master_ui = st.data_editor(pd.DataFrame(m_data) if m_data else pd.DataFrame({"Product": [""]*15, "FOB Price": [0.0]*15, "Origin": [""]*15, "Availability": ["Prompt"]*15, "Ship Time": ["Prompt"]*15}), use_container_width=True, num_rows="dynamic", key="m_edit_ui")
    with col_b:
        s_data = saved_config.get("spec_data", [])
        df_spec_ui = st.data_editor(pd.DataFrame(s_data) if s_data else pd.DataFrame({"Product": [""]*10, "FOB Price": [0.0]*10, "Origin": [""]*10, "Availability": ["Prompt"]*10, "Ship Time": ["Prompt"]*10}), use_container_width=True, num_rows="dynamic", key="s_edit_ui")

    st.markdown("---")
    inc_m = st.toggle("Include Standards", value=True)
    inc_s = st.toggle("Include Specialties", value=True)
    target_city = st.selectbox("Quick Single Target", active_cities) if active_cities else None
    
    if st.button(f"Generate Text Quote", type="primary"):
        if not target_city: st.error("Add cities in the sidebar first.")
        else:
            with st.spinner(f"Pricing {target_city}..."):
                res_text = run_calculation(target_city, df_master_ui, df_spec_ui, rate_map, round_val, inc_m, inc_s)
                if res_text:
                    st.subheader(f"Output for {target_city}:")
                    st.code(res_text, language="text") # This box is the primary tool now

with tab_bulk:
    st.header("Bulk Distribution Generator")
    if st.button("🚀 RUN FULL MARKET SHEET"):
        if not active_cities: st.error("City List is empty.")
        else:
            bulk_output = []
            progress = st.progress(0)
            for i, city in enumerate(active_cities):
                q_text = run_calculation(city, df_master_ui, df_spec_ui, rate_map, round_val, inc_m, inc_s)
                if q_text: bulk_output.append(q_text + "\n\n" + ("=" * 66) + "\n\n")
                progress.progress((i+1)/len(active_cities))
            if bulk_output:
                final_bulk = "".join(bulk_output)
                st.code(final_bulk, language="text")
                st.download_button("Download Market Sheet", final_bulk, file_name="Market_Quote.txt")

with tab_customers:
    st.header(f"Cloud CRM: {current_profile}")
    try: crm_all = conn.read(worksheet="CRM")
    except: crm_all = pd.DataFrame(columns=["profile_name", "Company Name", "Location", "Notes", "Buyer Email"])
    profile_crm = crm_all[crm_all['profile_name'] == current_profile]
    
    col_dir, col_prep = st.columns([2, 1])
    with col_prep:
        st.subheader("📬 Quick Email")
        if not profile_crm.empty:
            cust_name = st.selectbox("Select Customer", ["-- Select --"] + list(profile_crm["Company Name"].unique()))
            if cust_name != "-- Select --":
                c_row = profile_crm[profile_crm["Company Name"] == cust_name].iloc[0]
                if st.button("🚀 PREPARE EMAIL"):
                    with st.spinner("Pricing..."):
                        q_text = run_calculation(c_row['Location'], df_master_ui, df_spec_ui, rate_map, round_val, True, True)
                        if q_text:
                            email_addr = str(c_row.get('Buyer Email', ''))
                            mailto = f"mailto:{email_addr}?subject={urllib.parse.quote(f'Lumber Quote - {cust_name}')}&body={urllib.parse.quote(q_text)}"
                            st.markdown(f'<a href="{mailto}" target="_blank" style="text-decoration:none;"><div style="background-color:#0078d4;color:white;padding:15px;text-align:center;border-radius:8px;font-weight:bold;">OPEN IN OUTLOOK</div></a>', unsafe_allow_html=True)

    with col_dir:
        edited_crm = st.data_editor(
            profile_crm if not profile_crm.empty else pd.DataFrame(columns=["profile_name", "Company Name", "Location", "Notes", "Buyer Email"]),
            use_container_width=True, num_rows="dynamic", key="crm_edit_ui",
            column_order=("Company Name", "Location", "Notes", "Buyer Email"),
            column_config={"profile_name": None, "Buyer Email": st.column_config.TextColumn("Buyer Email")}
        )
        if st.button("💾 SAVE CRM"):
            gc = get_gspread_client()
            sh = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"])
            ws = sh.worksheet("CRM")
            edited_crm['profile_name'] = current_profile
            final_crm = pd.concat([crm_all[crm_all['profile_name'] != current_profile], edited_crm], ignore_index=True).fillna("")
            ws.clear()
            ws.update([final_crm.columns.values.tolist()] + final_crm.astype(str).values.tolist())
            st.success("CRM Synced!")
            st.cache_data.clear(); time.sleep(1); st.rerun()

# --- SIDEBAR: SAVE & DELETE ---
st.sidebar.markdown("---")
col_save, col_del = st.sidebar.columns(2)
if col_save.button("☁️ SAVE"):
    if len(user_pin) < 4: st.sidebar.error("Set PIN first.")
    else:
        gc = get_gspread_client()
        sh = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"])
        ws = sh.worksheet("Profiles")
        config = {
            "pin": user_pin, "states": states_input, "rates": rates_input, "sh_threshold": sh_threshold, 
            "sh_floor": sh_floor, "uni_div": uni_div, "msr_div": msr_div,
            "round_to": round_val, "cities_list": cities_list_raw,
            "master_data": df_master_ui.fillna("").to_dict('records'), "spec_data": df_spec_ui.fillna("").to_dict('records')
        }
        profiles_data = ws.get_all_records()
        f_row = next((i + 2 for i, row in enumerate(profiles_data) if row['profile_name'] == current_profile), -1)
        if f_row != -1: ws.update_cell(f_row, 2, json.dumps(config))
        else: ws.append_row([current_profile, json.dumps(config)])
        st.sidebar.success(f"Saved: {current_profile}")
        st.cache_data.clear(); time.sleep(1); st.rerun()

confirm_del = st.sidebar.checkbox("Confirm Delete")
if col_del.button("🗑️ DELETE"):
    if confirm_del and current_profile != "Default":
        gc = get_gspread_client()
        sh = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"])
        ws = sh.worksheet("Profiles")
        cell = ws.find(current_profile)
        if cell:
            ws.delete_rows(cell.row)
            st.sidebar.warning("Deleted.")
            st.cache_data.clear(); time.sleep(1); st.rerun()

try:
    m_count = len(conn.read(worksheet="Mileage"))
    st.sidebar.caption(f"📍 Mileage Atlas: {m_count} lanes cached")
except: pass