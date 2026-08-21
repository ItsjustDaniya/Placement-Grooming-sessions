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

Ported from the original Colab notebook cell. Logic is unchanged; only the
auth (Metabase session + Google Sheets) was swapped for headless
service-account / env-var based auth so this can run in GitHub Actions.
"""

import os
import sys

import pandas as pd
import requests
from gspread_dataframe import set_with_dataframe

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.metabase_auth import get_metabase_token
from common.sheets_auth import get_client

gc = get_client()
token = get_metabase_token()


# ─────────────────────────────────────────────────────────────────────────────
# Fetch lecture ratings data (Metabase card 7577)
# ─────────────────────────────────────────────────────────────────────────────
res = requests.post(
    "https://metabase-lierhfgoeiwhr.newtonschool.co/api/card/7577/query/json",
    headers={"Content-Type": "application/json", "X-Metabase-Session": token},
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
    "https://metabase-lierhfgoeiwhr.newtonschool.co/api/card/6289/query/json",
    headers={"Content-Type": "application/json", "X-Metabase-Session": token},
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
workbook = gc.open("Groomers")
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
sheet = gc.open_by_key("15vBCjR4Jj86ssSEkEg6CfCZJ3UCShZDUmKjTn3Ytniw")
worksheet = sheet.worksheet("Placement_grooming")
worksheet.clear()
set_with_dataframe(worksheet, final_df, include_index=False, include_column_header=True)
print("✓ Written to 'Placement_grooming'")

sheet = gc.open_by_key("1sUCcOrOgvZJc66Oyqjqo_ranftT1QApXoexKGdx3NG8")
worksheet = sheet.worksheet("Grooming- Human")
worksheet.clear()
set_with_dataframe(worksheet, final_df, include_index=False, include_column_header=True)
print("✓ Written to 'Grooming- Human'")

final_df_intake = final_df[final_df["call_type"] == "Intake Interiew"]
sheet = gc.open_by_key("1sUCcOrOgvZJc66Oyqjqo_ranftT1QApXoexKGdx3NG8")
worksheet = sheet.worksheet("Grooming - Human Call type Intake ")
worksheet.clear()
set_with_dataframe(worksheet, final_df_intake, include_index=False, include_column_header=True)
print(f"✓ Written to 'Grooming - Human Call type Intake ' ({len(final_df_intake):,} rows)")
