# Simple tests for data_quality_toolkit.py
# Run with: python test_tool.py
# Uses plain assert statements on a small made-up dataset, so the checks
# can be verified by hand and there is no dependency on a testing framework.
# Tests the library directly, since both command-line scripts are thin
# wrappers around it — if the library is correct, so are they.

import pandas as pd
import data_quality_toolkit as dqt


def make_test_df():
    return pd.DataFrame({
        'ID': [1, 2, 3, 4, 5],
        'Category': ['Cash', 'cash', 'Card', 'Card', 'UNKNOWN'],
        'Quantity': [2, 3, 1, 5, 2],
        'Price': [10, 10, 10, 10, 10],
        'Total': [20, 30, 10, 999, 20],  # row 4 is wrong on purpose: 5 x 10 should be 50, not 999
        'Date': ['2024-01-01', '2024-01-02', 'not a date', '2024-01-04', '2024-01-05']
    })


def test_missing_values_detected():
    df = make_test_df()
    report = dqt.quality_report(df, verbose=False)
    # "UNKNOWN" in Category should count as missing, even though it is not blank
    assert report['columns']['Category']['disguised_missing'] == 1
    print('PASS: disguised missing values are detected')


def test_cross_column_check_catches_the_wrong_row():
    df = make_test_df()
    checks = [{'col_a': 'Quantity', 'col_b': 'Price', 'col_c': 'Total'}]
    report = dqt.quality_report(df, cross_checks=checks, verbose=False)
    assert len(report['cross_column_checks']) == 1
    assert report['cross_column_checks'][0]['mismatches_found'] == 1
    print('PASS: cross-column check finds the row that does not add up')


def test_text_consistency_catches_the_casing_difference():
    df = make_test_df()
    report = dqt.quality_report(df, text_consistency_columns=['Category'], verbose=False)
    category_check = report['text_consistency'].get('Category')
    assert category_check is not None
    assert category_check['inconsistent_value_groups'] == 1
    print('PASS: text consistency check finds "Cash" vs "cash"')


def test_date_check_catches_the_bad_date():
    df = make_test_df()
    report = dqt.quality_report(df, date_columns=['Date'], verbose=False)
    date_check = report['date_checks'].get('Date')
    assert date_check is not None
    assert date_check['failed_to_parse'] == 1
    print('PASS: date check finds the value that is not a valid date')


def test_fill_missing_median_is_safe_on_text_columns():
    # a numeric strategy on a non-numeric column should not corrupt real data
    df = pd.DataFrame({'City': ['Kyiv', 'Lviv', None, 'Kyiv']})
    result = dqt.fill_missing(df, 'City', strategy='median', verbose=False)
    assert result['City'].iloc[0] == 'Kyiv'
    assert result['City'].iloc[1] == 'Lviv'
    assert pd.isna(result['City'].iloc[2])
    assert result['City'].iloc[3] == 'Kyiv'
    print('PASS: median strategy refuses to overwrite a non-numeric column')


def test_flag_outliers_does_not_change_existing_values():
    df = pd.DataFrame({'Amount': [10, 12, 11, 13, 500]})
    result = dqt.flag_outliers(df, 'Amount', verbose=False)
    assert result['Amount'].tolist() == [10, 12, 11, 13, 500]
    assert result['Amount_is_outlier'].tolist() == [False, False, False, False, True]
    print('PASS: outlier flagging adds a flag column without changing the data')


def test_standardise_casing_keeps_the_more_common_spelling():
    df = pd.DataFrame({'Payment': ['Cash', 'cash', 'Cash', 'Card']})
    result = dqt.standardise_casing(df, 'Payment', verbose=False)
    assert result['Payment'].tolist() == ['Cash', 'Cash', 'Cash', 'Card']
    print('PASS: text casing is standardised to the most common spelling')


if __name__ == '__main__':
    test_missing_values_detected()
    test_cross_column_check_catches_the_wrong_row()
    test_text_consistency_catches_the_casing_difference()
    test_date_check_catches_the_bad_date()
    test_fill_missing_median_is_safe_on_text_columns()
    test_flag_outliers_does_not_change_existing_values()
    test_standardise_casing_keeps_the_more_common_spelling()
    print()
    print('All tests passed.')
