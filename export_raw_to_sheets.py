#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ambil semua file dari repo GitHub dan upload daftar RAW URL ke Google Sheets.
Target repo: https://github.com/uppermoon77/oganic (branch main)

ENV yang didukung:
- GOOGLE_APPLICATION_CREDENTIALS : path ke service account JSON
- SPREADSHEET_NAME               : nama file Spreadsheet (default: 'Magelife RAW URLs')
- WORKSHEET_NAME                 : nama sheet/tab (default: 'urls')
- SHARE_WITH_EMAIL               : email yang akan di-share (opsional)
- OWNER, REPO, BRANCH, SUBDIR    : override target repo/folder
- RAW_STYLE                      : 'refs' (default) atau 'plain'
"""

import os
import sys
import requests
import pandas as pd
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from gspread_formatting import set_frozen, set_column_width, format_cell_ranges, CellFormat, TextFormat

# ==========================
# KONFIGURASI DEFAULT
# ==========================
OWNER   = os.getenv("OWNER",  "uppermoon77")
REPO    = os.getenv("REPO",   "oganic")
BRANCH  = os.getenv("BRANCH", "main")
SUBDIR  = os.getenv("SUBDIR", "")  # kosong = seluruh repo
RAW_STYLE = os.getenv("RAW_STYLE", "refs")  # 'refs' (sesuai contoh) atau 'plain'

SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Magelife RAW URLs")
WORKSHEET_NAME   = os.getenv("WORKSHEET_NAME",   "urls")
SHARE_WITH_EMAIL = os.getenv("SHARE_WITH_EMAIL")  # opsional: email kamu utk auto-share

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive"
]

# ==========================
# GITHUB HELPERS
# ==========================
def github_headers():
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "raw-url-exporter"
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_PAT")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def build_raw_url(owner, repo, branch, path, style="refs"):
    path = path.lstrip("/")
    if style == "refs":
        return f"https://raw.githubusercontent.com/{owner}/{repo}/refs/heads/{branch}/{path}"
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"

def fetch_git_tree(owner, repo, branch, recursive=True):
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}"
    if recursive:
        url += "?recursive=1"
    r = requests.get(url, headers=github_headers(), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Gagal ambil tree: {r.status_code} {r.text}")
    data = r.json()
    if "tree" not in data:
        raise RuntimeError("Response tidak ada 'tree'")
    return data["tree"]

# ==========================
# SHEETS HELPERS
# ==========================
def get_gspread_client():
    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path or not Path(cred_path).exists():
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS belum di-set atau file tidak ditemukan.")
    creds = Credentials.from_service_account_file(cred_path, scopes=SCOPES)
    return gspread.authorize(creds)

def open_or_create_spreadsheet(gc, name):
    # Coba buka kalau sudah ada
    try:
        return gc.open(name)
    except gspread.SpreadsheetNotFound:
        # Buat baru kalau belum ada
        sh = gc.create(name)
        return sh

def ensure_worksheet(sh, title):
    try:
        ws = sh.worksheet(title)
        # Bersihkan konten untuk overwrite rapi
        ws.clear()
        return ws
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=100, cols=6)

def share_if_needed(sh, email):
    if not email:
        return
    # Share editable ke email kamu
    try:
        sh.share(email, perm_type="user", role="writer", notify=True)
    except Exception as e:
        print(f"[!] Gagal share ke {email}: {e}")

def write_dataframe_with_hyperlinks(ws, df):
    """
    Tulis header + data, kolom 'raw_link' sebagai formula HYPERLINK.
    """
    headers = ["file_path", "raw_url", "raw_link", "size_bytes", "sha"]
    ws.update([headers])

    values = []
    for _, row in df.iterrows():
        # Formula HYPERLINK untuk Sheets
        link_formula = f'=HYPERLINK("{row["raw_url"]}", "Open RAW")'
        values.append([
            row["file_path"],
            row["raw_url"],
            link_formula,
            row.get("size_bytes", ""),
            row.get("sha", "")
        ])

    # Tulis mulai dari sel A2
    ws.update(f"A2:E{len(values)+1}", values, value_input_option="USER_ENTERED")

    # Freeze header + sedikit formatting
    try:
        set_frozen(ws, rows=1, cols=0)
        # Lebarkan kolom-kolom
        set_column_width(ws, 'A', 400)  # file_path
        set_column_width(ws, 'B', 450)  # raw_url
        set_column_width(ws, 'C', 120)  # Open RAW
        set_column_width(ws, 'D', 120)  # size
        set_column_width(ws, 'E', 260)  # sha
        # Bold header
        format_cell_ranges(ws, [
            ('A1:E1', CellFormat(textFormat=TextFormat(bold=True)))
        ])
    except Exception as e:
        print(f"[i] Formatting warning: {e}")

def main():
    print(f"[i] Ambil daftar file dari {OWNER}/{REPO}@{BRANCH} ...")
    tree = fetch_git_tree(OWNER, REPO, BRANCH, recursive=True)

    rows = []
    for node in tree:
        if node.get("type") != "blob":
            continue
        path = node.get("path", "")
        if SUBDIR:
            base = SUBDIR.rstrip("/")
            if not (path == base or path.startswith(base + "/")):
                continue
        raw = build_raw_url(OWNER, REPO, BRANCH, path, style=RAW_STYLE)
        rows.append({
            "file_path": path,
            "raw_url": raw,
            "size_bytes": node.get("size", None),
            "sha": node.get("sha", "")
        })

    if not rows:
        print("[!] Tidak ada file ditemukan. Cek SUBDIR/branch.")
        sys.exit(1)

    df = pd.DataFrame(rows).sort_values("file_path").reset_index(drop=True)

    # === Upload ke Google Sheets ===
    gc = get_gspread_client()
    sh = open_or_create_spreadsheet(gc, SPREADSHEET_NAME)
    share_if_needed(sh, SHARE_WITH_EMAIL)
    ws = ensure_worksheet(sh, WORKSHEET_NAME)
    write_dataframe_with_hyperlinks(ws, df)

    print("===============================================")
    print("[✓] Selesai upload ke Google Sheets.")
    print(f"Spreadsheet : {SPREADSHEET_NAME}")
    print(f"Worksheet   : {WORKSHEET_NAME}")
    print("Kolom 'Open RAW' bisa diklik untuk buka konten RAW.")
    print("===============================================")
    print("Tips: Jika spreadsheet belum terbuka oleh service account,")
    print("      share spreadsheet (kanan atas) ke email service account.")
    print("      Atau set env SHARE_WITH_EMAIL untuk auto-share.")

if __name__ == "__main__":
    main()
