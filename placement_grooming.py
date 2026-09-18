#!/usr/bin/env python3
"""
Placement Grooming Report
==========================
Pulls lecture-feedback ratings and batch info from Metabase, merges in
placement-grooming tracking data from the 'Groomers' Google Sheet, and
writes the combined result to three destination tabs:

  1. 'Placement_grooming' tab in spreadsheet 15vBCjR4Jj86ssSEkEg6CfCZJ3UCShZDUmKjTn3Ytniw
  2. 'Grooming- Human' tab in spreadsheet 1sUCcOrOgvZJc66Oyqjqo_ranftT1QApXoexKGdx3NG8
  3. 'Grooming - Human Call type Intake ' tab in the same spreadsheet as #2,
     filtered to call_type == 'Intake Interiew'

Ported from the original Colab notebook cell. Logic is unchanged. Auth was
reworked twice from the original:
  - Google Sheets: no longer imports common.sheets_auth.get_client() — auth
    is now inline via SERVICE_ACCOUNT_JSON (same pattern as the other
    pipeline scripts), so this file has no cross-module dependency.
  - Metabase: no longer imports common.metabase_auth.get_metabase_token()
    (username/password session login) — auth is now a static METABASE_API_KEY
    header (X-Api-Key), so there's no login step or token to refresh.
  - Added the same retry-hardened requests.Session and safe_open_sheet() /
    safe_open_by_key() wrappers used in the other pipeline scripts, since
    this file didn't have either before.
"""

import os
import sys
import json
import time
import traceback

import pandas as pd
import requests
import gspread
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from gspread_dataframe import set_with_dataframe
from google.oauth2.service_account import Credentials

start_time = time.time()

# -------------------- ENV & AUTH --------------------
METABASE_API_KEY = os.getenv("METABASE_API_KEY")
service_account_json = os.getenv("SERVICE_ACCOUNT_JSON")

missing = [n for n, v in [
    ("METABASE_API_KEY", METABASE_API_KEY),
    ("SERVICE_ACCOUNT_JSON", service_account_json),
] if not v]
if missing:
    raise ValueError(f"❌ Missing environment variables: {', '.join(missing)}")

service_info = json.loads(service_account_json)
creds = Credentials.from_service_account_info(
    service_info,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ],
)
gc = gspread.authorize(creds)

METABASE_BASE = "https://metabase-lierhfgoeiwhr.newtonschool.co"

# -------------------- RETRY-HARDENED SESSION --------------------
# Same fix as the other pipeline scripts: ConnectionError / ECONNRESET, 429,
# and 5xx are retried at the transport level instead of failing the whole
# job on the first hiccup.
SESSION = requests.Session()
_adapter = HTTPAdapter(
    max_retries=Retry(
        total=4,
        connect=4,
        read=2,
        backoff_factor=5,             # 5s, 10s, 20s, 40s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["POST", "GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    ),
    pool_connections=10,
    pool_maxsize=10,
)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)
requests.post = SESSION.post

# Static header used for every Metabase API call — no login step, no
# token expiry/refresh to worry about.
METABASE_HEADERS = {
    "Content-Type": "application/json",
    "X-Api-Key": METABASE_API_KEY,
}


def safe_open_sheet(title):
    """gc.open() wrapped to fail with an actionable message (the exact
    service-account email to share the sheet with) instead of a bare
    SpreadsheetNotFound traceback."""
    try:
        return gc.open(title)
    except gspread.exceptions.SpreadsheetNotFound:
        raise RuntimeError(
            f"❌ Could not open Google Sheet '{title}'. Either the title "
            f"doesn't match exactly, or it hasn't been shared with this "
            f"service account: {service_info.get('client_email')}. "
            "Share it as Editor, then re-run."
        )


def safe_open_by_key(key):
    """Same as safe_open_sheet, but for gc.open_by_key()."""
    try:
        return gc.open_by_key(key)
    except gspread.exceptions.SpreadsheetNotFound:
        raise RuntimeError(
            f"❌ Could not open Google Sheet with key '{key}'. Share it with "
            f"this service account as Editor: {service_info.get('client_email')}"
        )


print("🔎 ENV CHECK")
print(f"   Metabase API key   : {'[SET]' if METABASE_API_KEY else '[MISSING]'}")
print(f"   SA client_email    : {service_info.get('client_email')}")

try:
    # ─────────────────────────────────────────────────────────────────────────────
    # Fetch lecture ratings data (Metabase card 7577)
    # ─────────────────────────────────────────────────────────────────────────────
    res = requests.post(
        f"{METABASE_BASE}/api/card/7577/query/json",
        headers=METABASE_HEADERS,
    )
    res.raise_for_status()
    df = pd.DataFrame(res.json())
    print(f"✓ Fetched {len(df):,} rows from card 7577")

    df["business_acumen_1"] = df["business_acumen"].str.extract(r"(\d+)").astype(float).astype("Int64")
    df["communication_rating_1"] = df["communication_rating"].str.extract(r"(\d+)").astype(float).astype("Int64")
    df["self_intro_1"] = df["self_intro"].str.extract(r"(\d+)").astype(float).astype("Int64")
    df["project_explanation_1"] = df["project_explanation"].str.extract(r"(\d+)").astype(float).astype("Int64")
    df["pace_speed_1"] = df["pace_speed"].str.extract(r"(\d+)").astype(float).astype("Int64")
    df["job_readiness_1"] = df["job_readiness"].str.extract(r"(\d+)").astype(float).astype("Int64")
    df["student_intent_1"] = df["student_intent"].str.extract(r"(\d+)").astype(float).astype("Int64")
    df["excel_1"] = df["excel_overall_proficiency"].str.extract(r"(\d+)").astype(float).astype("Int64")
    df["power_bi_1"] = df["pbi_overall_proficiency"].str.extract(r"(\d+)").astype(float).astype("Int64")
    df["sql_1"] = df["sql_overall_proficiency"].str.extract(r"(\d+)").astype(float).astype("Int64")
    df["hr_questions_1"] = df["hr_questions"].str.extract(r"(\d+)").astype(float).astype("Int64")

    rating_columns = [
        "business_acumen_1", "communication_rating_1", "self_intro_1", "project_explanation_1",
        "pace_speed_1", "job_readiness_1", "student_intent_1", "excel_1",
        "power_bi_1", "sql_1", "hr_questions_1",
    ]

    df["consolidated_rating"] = df[rating_columns].mean(axis=1)
    df["consolidated_rating"] = (df["consolidated_rating"] / df[rating_columns].max().max()) * 5
    df["consolidated_rating"] = df["consolidated_rating"].round(2)


    def calculate_average(text):
        try:
            if text.strip() == "":
                return None
            numbers = list(map(int, text.split(" - ")))
            return int(sum(numbers) / len(numbers))
        except (ValueError, AttributeError):
            return None


    df["pr_conversion_weeks_1"] = df["pr_conversion_weeks"].apply(calculate_average)
    df = df.rename(columns={"student_id": "user_id"})


    # ─────────────────────────────────────────────────────────────────────────────
    # Fetch batch names (Metabase card 6289) and merge
    # ─────────────────────────────────────────────────────────────────────────────
    res3 = requests.post(
        f"{METABASE_BASE}/api/card/6289/query/json",
        headers=METABASE_HEADERS,
    )
    res3.raise_for_status()
    df3 = pd.DataFrame(res3.json())
    print(f"✓ Fetched {len(df3):,} rows from card 6289")

    df3 = df3[["user_id", "au_batch_name"]]
    final_df = pd.merge(df, df3, on="user_id", how="left")

    final_df["primary_key"] = final_df["session_id"].astype(str) + final_df["user_id"].astype(str)


    # ─────────────────────────────────────────────────────────────────────────────
    # Read grooming/placement tracking data from Google Sheets
    # ─────────────────────────────────────────────────────────────────────────────
    workbook = safe_open_sheet("Groomers")
    worksheet1 = workbook.worksheet("Groomers")
    data1 = worksheet1.get_all_values()
    groomers_df = pd.DataFrame(data1)
    groomers_df.columns = groomers_df.iloc[0]
    groomers_df = groomers_df.iloc[1:]
    groomers_df = groomers_df.rename(columns={"UserID": "user_id"})
    df_1 = groomers_df[[
        "user_id", "Grooming Pool (Picked)", "Placed", "PR",
        "Phase", "Audit", "Recommended date",
        "Status", "Picked Date", "Date of PR", "Placement Month",
        "Recommended Pool (PI)", "Debarred Date",
    ]]

    df_1["user_id"] = pd.to_numeric(df_1["user_id"], errors="coerce")
    df_1 = df_1.dropna(subset=["user_id"])
    df_1["user_id"] = df_1["user_id"].astype(int)
    df_1 = df_1.drop_duplicates(subset="user_id", keep="first")

    print(f"df_1 unique user_ids: {df_1['user_id'].is_unique}")
    print(f"df_1 shape: {df_1.shape}")

    final_df["user_id"] = final_df["user_id"].replace({",": ""}, regex=True).astype(int)

    columns_to_map = [
        "Grooming Pool (Picked)", "Placed", "PR", "Phase", "Audit",
        "Status", "Picked Date", "Date of PR", "Placement Month", "Recommended date",
        "Recommended Pool (PI)", "Debarred Date",
    ]
    for column in columns_to_map:
        column_dict = df_1.set_index("user_id")[column].to_dict()
        final_df[column] = final_df["user_id"].map(column_dict)
        final_df[column] = final_df[column].fillna("No Data")

    final_df = final_df.drop_duplicates()
    print(f"✓ final_df: {len(final_df):,} rows")


    # ─────────────────────────────────────────────────────────────────────────────
    # Write outputs
    # ─────────────────────────────────────────────────────────────────────────────
    sheet = safe_open_by_key("15vBCjR4Jj86ssSEkEg6CfCZJ3UCShZDUmKjTn3Ytniw")
    worksheet = sheet.worksheet("Placement_grooming")
    worksheet.clear()
    set_with_dataframe(worksheet, final_df, include_index=False, include_column_header=True)
    print("✓ Written to 'Placement_grooming'")

    sheet = safe_open_by_key("1sUCcOrOgvZJc66Oyqjqo_ranftT1QApXoexKGdx3NG8")
    worksheet = sheet.worksheet("Grooming- Human")
    worksheet.clear()
    set_with_dataframe(worksheet, final_df, include_index=False, include_column_header=True)
    print("✓ Written to 'Grooming- Human'")

    final_df_intake = final_df[final_df["call_type"] == "Intake Interiew"]
    sheet = safe_open_by_key("1sUCcOrOgvZJc66Oyqjqo_ranftT1QApXoexKGdx3NG8")
    worksheet = sheet.worksheet("Grooming - Human Call type Intake ")
    worksheet.clear()
    set_with_dataframe(worksheet, final_df_intake, include_index=False, include_column_header=True)
    print(f"✓ Written to 'Grooming - Human Call type Intake ' ({len(final_df_intake):,} rows)")

except Exception as e:
    print(f"❌ Pipeline failed: {e}")
    traceback.print_exc()
    sys.exit(1)

mins, secs = divmod(time.time() - start_time, 60)
print(f"\n🎯 Placement Grooming Report completed successfully in {int(mins)}m {int(secs)}s")
sys.exit(0)
