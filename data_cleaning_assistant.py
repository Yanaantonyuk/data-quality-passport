# Data Cleaning Assistant — command-line version
# Runs the cleaning functions from data_quality_toolkit.py on a whole file,
# using a strategy you choose per column, and saves a cleaned file plus a
# log of exactly what changed. Never overwrites the original file.
#
# If you are working in a notebook, you probably want to import
# fill_missing(), flag_outliers(), standardise_casing() and
# standardise_dates() from data_quality_toolkit.py directly instead of
# running this script — see the README for examples.

import pandas as pd
import json
import os
import sys
import argparse
import data_quality_toolkit as dqt

# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------

INPUT_FILE = 'dirty_cafe_sales.csv'
SHEET_NAME = None  # for Excel files with more than one sheet

OUTPUT_CSV = 'cleaned_data.csv'
OUTPUT_LOG = 'cleaning_log.json'

# strategy per column. Four reserved words:
#   'median'    - fill missing values with the column median (numbers only)
#   'mode'      - fill missing values with the most common value in the column
#   'drop'      - remove rows where this column is missing
#   'flag_only' - leave missing values as they are, just count and log them
# anything else you type is used as a literal fill value, for example:
#   'Category': 'Unknown'
CLEANING_STRATEGY = {
    'Item': 'mode',
    'Quantity': 'median',
    'Price Per Unit': 'median',
    'Total Spent': 'median',
    'Payment Method': 'Unknown',
    'Location': 'Unknown',
    'Transaction Date': 'flag_only'
}

# strategy used for any column not listed above
DEFAULT_STRATEGY = 'flag_only'

# columns to standardise casing on, e.g. "cash" and "Cash" both become "Cash"
# set to 'auto' to apply this to every text column, or a list of column names
TEXT_CASING_COLUMNS = 'auto'

# columns to convert to a consistent ISO date format (YYYY-MM-DD)
DATE_COLUMNS = ['Transaction Date']

# numeric columns to flag outliers on, using the IQR method
# this only adds a flag column, it never removes or changes the data itself
OUTLIER_FLAG_COLUMNS = ['Total Spent']


def apply_missing_value_strategies(df, log):
    rows_to_drop = pd.Series(False, index=df.index)

    for col in df.columns:
        strategy = CLEANING_STRATEGY.get(col, DEFAULT_STRATEGY)
        missing_mask = df[col].isnull()
        missing_count = int(missing_mask.sum())

        if missing_count == 0:
            continue

        if strategy == 'flag_only':
            log['columns'][col] = {'strategy': 'flag_only', 'missing_left_as_is': missing_count}

        elif strategy == 'drop':
            # dropping rows is a single pandas one-liner, so there is no
            # wrapper for it in the toolkit — just tracked here across
            # columns so multiple 'drop' columns do not conflict
            rows_to_drop = rows_to_drop | missing_mask
            log['columns'][col] = {'strategy': 'drop', 'rows_marked_for_removal': missing_count}

        else:
            df = dqt.fill_missing(df, col, strategy, verbose=False, log=log['columns'])

    rows_dropped = int(rows_to_drop.sum())
    if rows_dropped > 0:
        df = df[~rows_to_drop].reset_index(drop=True)
    log['rows_dropped'] = rows_dropped

    return df


def main():
    parser = argparse.ArgumentParser(description='Clean a CSV or Excel file based on per-column strategies.')
    parser.add_argument('--csv', help='path to the file to clean, overrides INPUT_FILE in the config section')
    parser.add_argument('--sheet', help='sheet name to check, for Excel files with more than one sheet')
    args = parser.parse_args()

    input_file_to_use = args.csv if args.csv else INPUT_FILE
    sheet_name_to_use = args.sheet if args.sheet else SHEET_NAME

    try:
        print('Step 1: loading the file and replacing disguised missing values...')
        df = dqt.load_dataset(input_file_to_use, sheet_name_to_use)
    except (FileNotFoundError, ValueError) as error:
        print(str(error))
        sys.exit(1)

    disguised_count = int(sum(df[col].isin(dqt.DISGUISED_MISSING).sum() for col in df.columns))
    df = dqt.fill_disguised_missing(df, verbose=False)

    log = {
        'source_file': os.path.basename(input_file_to_use),
        'starting_shape': list(df.shape),
        'disguised_missing_values_converted': disguised_count,
        'columns': {}
    }

    print('Step 2: applying missing value strategies...')
    df = apply_missing_value_strategies(df, log)

    print('Step 3: standardising text casing...')
    log['text_casing'] = {}
    columns_for_casing = [c for c in df.columns if pd.api.types.is_string_dtype(df[c])] if TEXT_CASING_COLUMNS == 'auto' else TEXT_CASING_COLUMNS
    for col in columns_for_casing:
        if col in df.columns:
            df = dqt.standardise_casing(df, col, verbose=False, log=log['text_casing'])

    print('Step 4: standardising date formats...')
    log['date_formatting'] = {}
    for col in DATE_COLUMNS:
        if col in df.columns:
            df = dqt.standardise_dates(df, col, verbose=False, log=log['date_formatting'])

    print('Step 5: flagging outliers (not removing them)...')
    log['outlier_flags'] = {}
    for col in OUTLIER_FLAG_COLUMNS:
        if col in df.columns:
            df = dqt.flag_outliers(df, col, verbose=False, log=log['outlier_flags'])

    log['final_shape'] = list(df.shape)

    df.to_csv(OUTPUT_CSV, index=False)
    with open(OUTPUT_LOG, 'w') as f:
        json.dump(log, f, indent=2)

    print()
    print('=== SUMMARY ===')
    print(f"Started with {log['starting_shape'][0]} rows, {log['starting_shape'][1]} columns.")
    print(f"Ended with {log['final_shape'][0]} rows, {log['final_shape'][1]} columns.")
    print(f"Rows dropped: {log['rows_dropped']}")
    print(f"Disguised missing values converted to NaN: {disguised_count}")
    print()
    print(f'Cleaned file saved to: {OUTPUT_CSV}')
    print(f'Full log saved to: {OUTPUT_LOG}')


if __name__ == '__main__':
    main()
