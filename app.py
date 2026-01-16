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
st.set_page_config(page_title="Lumber Hub: Regional Trading Desk", layout="wide")

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
current_profile = (new_profile if new_profile else selected_profile).strip()

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
    st.error("🔒 Profile Locked. Enter correct PIN in sidebar.")
    st.stop()

# --- SIDEBAR: SETTINGS ---
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
        headers = {'User-Agent': 'lumber_hub_v16'}
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
        
        stock_note = ""
        if stock == "Low": stock_note = " (LTD)"
        elif stock == "Out": stock_note = " (SO)"

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
    def prep_df_simple(data, rows):
        df = pd.DataFrame(data) if data else pd.DataFrame({"Include": [True]*rows, "Product": [""]*rows, "FOB Price": [0.0]*rows, "Origin": [""]*rows, "Stock": ["High"]*rows, "Availability": ["Prompt"]*rows, "Ship Time": ["Prompt"]*rows})
        if "Include" not in df.columns: df.insert(0, "Include", True)
        return df
    
    c_m1, c_m2 = st.columns(2)
    with c_m1:
        st.subheader("Standard Master")
        df_master_ui = st.data_editor(prep_df_simple(saved_config.get("master_data"), 15), use_container_width=True, num_rows="dynamic", key="m_edit", column_config={"Stock": st.column_config.SelectboxColumn(options=["High", "Low", "Out"])})
    with c_m2:
        st.subheader("Specialty Master")
        df_spec_ui = st.data_editor(prep_df_simple(saved_config.get("spec_data"), 10), use_container_width=True, num_rows="dynamic", key="s_edit", column_config={"Stock": st.column_config.SelectboxColumn(options=["High", "Low", "Out"])})
    
    st.markdown("---")
    st.subheader("📝 Quick Quote Prep")
    fc1, fc2, fc3 = st.columns([2, 1, 1])
    with fc1:
        target_city = st.selectbox("Quick Single Target", active_cities) if active_cities else None
    with fc2:
        inc_std = st.checkbox("Include Standard", value=True)
    with fc3:
        inc_spec = st.checkbox("Include Specialty", value=True)

    if st.button("Generate Quote"):
        dfs_to_calc = []
        if inc_std: dfs_to_calc.append(df_master_ui)
        if inc_spec: dfs_to_calc.append(df_spec_ui)
        
        if dfs_to_calc:
            res = run_calculation(target_city, pd.concat(dfs_to_calc), rate_map, round_val)
            if res: st.code(res, language="text")
        else:
            st.warning("Please select at least one inventory type.")

with tab_bulk:
    st.header("Bulk Market Sheets")
    bc1, bc2 = st.columns(2)
    with bc1:
        bulk_std = st.checkbox("Bulk: Include Standard", value=True, key="bulk_std")
    with bc2:
        bulk_spec = st.checkbox("Bulk: Include Specialty", value=True, key="bulk_spec")
        
    if st.button("🚀 RUN FULL MARKET SHEET"):
        bulk_output = []
        inventory_list = []
        if bulk_std: inventory_list.append(df_master_ui)
        if bulk_spec: inventory_list.append(df_spec_ui)
        
        if inventory_list:
            df_full_inventory = pd.concat(inventory_list)
            for city in active_cities:
                q = run_calculation(city, df_full_inventory, rate_map, round_val)
                if q: bulk_output.append(q + "\n\n" + ("=" * 66) + "\n\n")
            if bulk_output: st.code("".join(bulk_output), language="text")
        else:
            st.warning("Select inventory type to run the market sheet.")

with tab_customers:
    st.header(f"Cloud CRM: {current_profile}")
    try:
        crm_all = conn.read(worksheet="CRM").fillna("")
        needed_cols = ["profile_name", "Company Name", "Location", "Notes", "Buyer Email", "Last Quoted"]
        for col in needed_cols:
            if col not in crm_all.columns: crm_all[col] = ""
    except Exception:
        crm_all = pd.DataFrame(columns=["profile_name", "Company Name", "Location", "Notes", "Buyer Email", "Last Quoted"])
    
    profile_crm = crm_all[crm_all['profile_name'] == current_profile].reset_index(drop=True)
    
    col_dir, col_prep = st.columns([2, 1])
    with col_prep:
        st.subheader("📬 Prepare Quote")
        daily_blurb = st.text_area("Daily Blurb / Market Update", value=saved_config.get("daily_blurb", ""))
        
        if not profile_crm.empty:
            cust_name = st.selectbox("Select Customer", ["-- Select --"] + list(profile_crm["Company Name"].unique()))
            if cust_name != "-- Select --":
                c_row = profile_crm[profile_crm["Company Name"] == cust_name].iloc[0]
                locs_raw = str(c_row['Location'])
                loc_list = [l.strip() for l in locs_raw.split(';') if l.strip()]
                
                qc1, qc2 = st.columns(2)
                crm_std = qc1.checkbox("Outlook: Std", value=True)
                crm_spec = qc2.checkbox("Outlook: Spec", value=True)

                if st.button(f"Open Outlook Draft"):
                    ts = datetime.now().strftime("%m/%d %H:%M")
                    crm_all.loc[(crm_all['profile_name'] == current_profile) & (crm_all['Company Name'] == cust_name), "Last Quoted"] = ts
                    
                    multi_q_output = []
                    active_inv = []
                    if crm_std: active_inv.append(df_master_ui)
                    if crm_spec: active_inv.append(df_spec_ui)
                    
                    if active_inv:
                        df_full = pd.concat(active_inv)
                        for city in loc_list:
                            q = run_calculation(city, df_full, rate_map, round_val, True)
                            if q: multi_q_output.append(q)
                        
                        quotes_text = "\n\n---\n\n".join(multi_q_output)
                        final_body = f"{daily_blurb}\n\n{quotes_text}" if daily_blurb else quotes_text
                        
                        # UPDATED SUBJECT LOGIC
                        email_subject = f"Lumber - {datetime.now().strftime('%m/%d/%y')}"
                        mailto = f"mailto:{c_row['Buyer Email']}?subject={urllib.parse.quote(email_subject)}&body={urllib.parse.quote(final_body)}"
                        st.markdown(f'<a href="{mailto}" target="_blank"><div style="background-color:#0078d4;color:white;padding:15px;text-align:center;border-radius:8px;font-weight:bold;">OPEN IN OUTLOOK</div></a>', unsafe_allow_html=True)
                    else:
                        st.warning("Toggle Std or Spec.")

    with col_dir:
        edited_crm = st.data_editor(
            profile_crm, 
            use_container_width=True, 
            num_rows="dynamic", 
            key="crm_editor_v2", 
            column_order=("Company Name", "Location", "Last Quoted", "Notes", "Buyer Email"),
            column_config={
                "Location": st.column_config.TextColumn("Locations (Use ; to separate)"), 
                "Last Quoted": st.column_config.TextColumn("Last Quote", disabled=True),
                "Buyer Email": st.column_config.TextColumn("Email")
            }
        )
        
        if st.button("💾 SAVE CRM"):
            try:
                gc = get_gspread_client()
                sh = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"])
                ws = sh.worksheet("CRM")
                edited_crm['profile_name'] = current_profile
                other_profiles_crm = crm_all[crm_all['profile_name'] != current_profile]
                final_crm_df = pd.concat([other_profiles_crm, edited_crm], ignore_index=True)
                final_crm_df = final_crm_df[final_crm_df['Company Name'] != ""].astype(str)
                ws.clear()
                ws.update([final_crm_df.columns.values.tolist()] + final_crm_df.values.tolist())
                st.success("CRM Synced!")
                time.sleep(1)
                st.cache_data.clear()
                st.rerun()
            except Exception as e:
                st.error(f"Save Failed: {e}")

# --- SIDEBAR: SAVE PROFILE ---
if st.sidebar.button("☁️ SAVE PROFILE"):
    gc = get_gspread_client()
    sh = gc.open_by_url(st.secrets["connections"]["gsheets"]["spreadsheet"])
    ws = sh.worksheet("Profiles")
    config = {"pin": user_pin, "states": states_input, "rates": rates_input, "sh_threshold": sh_threshold, "sh_floor": sh_floor, "uni_div": uni_div, "msr_div": msr_div, "round_to": round_val, "cities_list": cities_list_raw, "daily_blurb": daily_blurb, "master_data": df_master_ui.to_dict('records'), "spec_data": df_spec_ui.to_dict('records')}
    profiles_data = ws.get_all_records()
    f_row = next((i + 2 for i, row in enumerate(profiles_data) if row['profile_name'] == current_profile), -1)
    if f_row != -1: ws.update_cell(f_row, 2, json.dumps(config))
    else: ws.append_row([current_profile, json.dumps(config)])
    st.cache_data.clear(); st.rerun()