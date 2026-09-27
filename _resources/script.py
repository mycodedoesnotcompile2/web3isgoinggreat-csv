#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import html
import json
import re
import sys
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup


FIRESTORE_URL = "https://firestore.googleapis.com/v1/projects/web3-334501/databases/(default)/documents:runQuery"
CSV_FILENAME = "web3isgoinggreat.csv"
PAGE_SIZE = 100

CSV_COLUMNS = [
    "id",
    "date",
    "title",
    "short_title",
    "readable_id",
    "body",
    "icon",
    "image_src",
    "image_alt",
    "image_caption",
    "image_is_logo",
    "image_class",
    "blockchain",
    "tech",
    "theme",
    "collection",
    "scam_amount_total",
    "scam_amount_has_scam_amount",
    "scam_amount_pre_recovery",
    "social_mastodon",
    "social_bluesky",
    "links",
    "create_time",
    "update_time",
]


def abs_file(filepath):
    return Path(filepath).expanduser().resolve()


def firestore_value(value):
    if not isinstance(value, dict):
        return value

    if "stringValue" in value:
        return value["stringValue"]

    if "integerValue" in value:
        return int(value["integerValue"])

    if "doubleValue" in value:
        return float(value["doubleValue"])

    if "booleanValue" in value:
        return value["booleanValue"]

    if "nullValue" in value:
        return None

    if "timestampValue" in value:
        return value["timestampValue"]

    if "referenceValue" in value:
        return value["referenceValue"]

    if "bytesValue" in value:
        return value["bytesValue"]

    if "geoPointValue" in value:
        return value["geoPointValue"]

    if "arrayValue" in value:
        return [firestore_value(v) for v in value["arrayValue"].get("values", [])]

    if "mapValue" in value:
        return {k: firestore_value(v) for k, v in value["mapValue"].get("fields", {}).items()}

    raise ValueError(f"Unknown Firestore value type: {list(value.keys())}")


def convert_document(document):
    record = {k: firestore_value(v) for k, v in document.get("fields", {}).items()}
    record["_create_time"] = document.get("createTime")
    record["_update_time"] = document.get("updateTime")
    return record


def clean_text(value):
    if not value:
        return ""

    value = html.unescape(str(value))

    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)

    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def clean_nested_strings(value):
    if isinstance(value, str):
        return clean_text(value)

    if isinstance(value, list):
        return [clean_nested_strings(item) for item in value]

    if isinstance(value, dict):
        return {key: clean_nested_strings(item) for key, item in value.items()}

    return value


def json_cell(value):
    if value is None or value == [] or value == {}:
        return ""

    value = clean_nested_strings(value)

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def flatten_record(record):
    image = record.get("image") or {}
    scam_amount = record.get("scamAmountDetails") or {}
    social_post_ids = record.get("socialPostIds") or {}
    filters = record.get("filters") or {}

    return {
        "id": record.get("id"),
        "date": record.get("date"),
        "title": clean_text(record.get("title")),
        "short_title": clean_text(record.get("shortTitle")),
        "readable_id": clean_text(record.get("readableId")),
        "body": clean_text(record.get("body")),
        "icon": record.get("icon", record.get("faicon")),
        "image_src": clean_text(image.get("src")),
        "image_alt": clean_text(image.get("alt")),
        "image_caption": clean_text(image.get("caption")),
        "image_is_logo": image.get("isLogo"),
        "image_class": clean_text(image.get("class")),
        "blockchain": json_cell(filters.get("blockchain")),
        "tech": json_cell(filters.get("tech")),
        "theme": json_cell(filters.get("theme")),
        "collection": json_cell(record.get("collection")),
        "scam_amount_total": scam_amount.get("total"),
        "scam_amount_has_scam_amount": scam_amount.get("hasScamAmount"),
        "scam_amount_pre_recovery": scam_amount.get("preRecoveryAmount"),
        "social_mastodon": clean_text(social_post_ids.get("mastodon")),
        "social_bluesky": clean_text(social_post_ids.get("bluesky")),
        "links": json_cell(record.get("links")),
        "create_time": record.get("_create_time"),
        "update_time": record.get("_update_time"),
    }


def fetch_page(session, cursor=None):
    query = {
        "structuredQuery": {
            "from": [{"collectionId": "entries"}],
            "orderBy": [
                {"field": {"fieldPath": "id"}, "direction": "DESCENDING"},
                {"field": {"fieldPath": "__name__"}, "direction": "DESCENDING"},
            ],
            "limit": PAGE_SIZE,
        }
    }

    if cursor:
        query["structuredQuery"]["startAt"] = {
            "before": False,
            "values": [{"stringValue": cursor}],
        }

    response = session.post(FIRESTORE_URL, json=query, timeout=30)
    response.raise_for_status()

    return [x["document"] for x in response.json() if "document" in x]


def fetch_all_entries():
    session = requests.Session()
    session.headers["User-Agent"] = "web3isgoinggreat-csv-exporter/1.0"

    records = []
    cursor = None
    page = 0

    while True:
        page += 1
        print(f"[*] Downloading page {page}...", file=sys.stderr)

        documents = fetch_page(session, cursor)

        if not documents:
            break

        page_records = [convert_document(document) for document in documents]
        records.extend(page_records)

        print(f"[+] Downloaded {len(records)} documents", file=sys.stderr)

        cursor = page_records[-1].get("id")

        if not cursor:
            raise RuntimeError("Last document does not contain an 'id' field")

        if len(documents) < PAGE_SIZE:
            break

    return records


def build_dataframe(records):
    df = pd.DataFrame([flatten_record(record) for record in records], columns=CSV_COLUMNS)

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values(by=["date", "id"], ascending=[True, True], na_position="last").reset_index(drop=True)
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    return df


def write_csv(df, output_file):
    df.to_csv(output_file, sep=";", quoting=csv.QUOTE_ALL, index=False, lineterminator="\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Export Web3 Is Going Great Firestore entries to CSV.")
    parser.add_argument("-o", "--output-file", type=abs_file, default=Path.cwd() / CSV_FILENAME, help=f"Output CSV file (default: ./{CSV_FILENAME})")
    options = parser.parse_args()

    print("[*] Downloading Web3 Is Going Great entries...")

    try:
        records = fetch_all_entries()
    except requests.RequestException as exc:
        print(f"[!] Firestore request failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except (ValueError, RuntimeError) as exc:
        print(f"[!] Unable to process Firestore data: {exc}", file=sys.stderr)
        sys.exit(1)

    if not records:
        print("[!] No entries were returned.")
        sys.exit(1)

    print(f"[+] {len(records)} Firestore entries downloaded")

    df = build_dataframe(records)
    write_csv(df, options.output_file)

    print(f"[+] Wrote {len(df)} rows to {options.output_file}")


if __name__ == "__main__":
    main()
