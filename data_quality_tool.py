# Data Quality Passport — command-line version
# Runs the full check-and-report pipeline from data_quality_toolkit.py on
# one file and saves a JSON report plus an HTML dashboard.
#
# If you are working in a notebook, you probably want to import the
# functions from data_quality_toolkit.py directly instead of running this
# script — see the README for examples.

import json
import os
import sys
import argparse
import data_quality_toolkit as dqt

# ------------------------------------------------------------------
# CONFIG — change these settings depending on the dataset you load
# ------------------------------------------------------------------

CSV_PATH = 'dirty_cafe_sales.csv'

# for Excel files with multiple sheets, set the sheet name to check
# leave as None to use the first sheet automatically
SHEET_NAME = None

# column used to check for repeated IDs
# set to a column name, or 'auto' to detect it automatically, or None to skip this check
ID_COLUMN = 'auto'

# list of multiplication checks: col_a x col_b should equal col_c
# add as many as you need, or set to [] to skip this check entirely
CROSS_CHECKS = [
    {'col_a': 'Quantity', 'col_b': 'Price Per Unit', 'col_c': 'Total Spent'}
]

# numeric columns to check for outliers using the IQR method
# set to a list of column names, or 'auto' to detect them automatically
OUTLIER_COLUMNS = 'auto'

# text columns to check for inconsistent casing, e.g. "cash" vs "Cash" vs "CASH"
# set to a list of column names, or 'auto' to detect them automatically
TEXT_CONSISTENCY_COLUMNS = 'auto'

# columns that should contain dates, checked for values that do not parse
# set to a list of column names, or 'auto' to detect them automatically
DATE_COLUMNS = 'auto'

OUTPUT_JSON = 'quality_report.json'
OUTPUT_HTML = 'data_quality_passport.html'


def main():
    parser = argparse.ArgumentParser(description='Check a CSV or Excel file for data quality problems.')
    parser.add_argument('--csv', help='path to the file to check, overrides CSV_PATH in the config section')
    parser.add_argument('--sheet', help='sheet name to check, for Excel files with more than one sheet')
    args = parser.parse_args()

    csv_path_to_use = args.csv if args.csv else CSV_PATH
    sheet_name_to_use = args.sheet if args.sheet else SHEET_NAME

    try:
        print('Step 1: running rule-based checks...')
        df = dqt.load_dataset(csv_path_to_use, sheet_name_to_use)
        report = dqt.quality_report(
            df,
            source_name=os.path.basename(csv_path_to_use),
            id_column=ID_COLUMN,
            cross_checks=CROSS_CHECKS,
            outlier_columns=OUTLIER_COLUMNS,
            text_consistency_columns=TEXT_CONSISTENCY_COLUMNS,
            date_columns=DATE_COLUMNS
        )
    except (FileNotFoundError, ValueError) as error:
        print(str(error))
        sys.exit(1)

    print()
    print('These were guessed automatically where set to \'auto\'. If any of them look wrong,')
    print('open this file, find the CONFIG section at the top, and replace \'auto\' with your')
    print('own list of column names for that setting. Then run the script again.')

    with open(OUTPUT_JSON, 'w') as f:
        json.dump(report, f, indent=2)

    print('Step 2: generating AI summary...')
    ai_summary_text = dqt.ai_summary(report)

    print('Step 3: building the dashboard...')
    dqt.generate_dashboard(report, ai_summary_text, OUTPUT_HTML)

    print()
    print('=== CONCLUSION ===')
    print(ai_summary_text)
    print()
    print(f'Full report saved to: {OUTPUT_JSON}')
    print(f'Dashboard saved to: {OUTPUT_HTML}')


if __name__ == '__main__':
    main()
