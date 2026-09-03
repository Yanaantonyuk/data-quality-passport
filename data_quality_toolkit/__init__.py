# Data Quality Toolkit
# A set of functions for checking and cleaning messy data, meant to be
# imported directly into your own notebook or script:
#
#   from data_quality_toolkit import load_dataset, quality_report, fill_disguised_missing
#
#   df = load_dataset('my_file.csv')
#   report = quality_report(df)
#   df = fill_disguised_missing(df)
#
# Library functions never call sys.exit() — they raise normal Python
# exceptions, so they behave the way you would expect inside a notebook.
# The command-line scripts (data_quality_tool.py, data_cleaning_assistant.py)
# are thin wrappers around this module for people who want to just run a
# script rather than write code.

import pandas as pd
import json
import os
import html

# ------------------------------------------------------------------
# DEFAULT SETTINGS — override these before calling a function if needed,
# e.g. data_quality_toolkit.DISGUISED_MISSING = ['UNKNOWN', 'N/A', 'TBD']
# ------------------------------------------------------------------

# text values that count as missing even though they are not blank
DISGUISED_MISSING = ['UNKNOWN', 'ERROR', 'N/A', 'NULL', 'None', '']

# columns that look like personal data never have raw example values shown,
# even if they end up in a text consistency check
SENSITIVE_KEYWORDS = ['name', 'email', 'phone', 'address', 'card', 'iban']

# the Claude model used for ai_summary()
# Anthropic retires old model versions over time — if this stops working,
# check https://docs.claude.com for the current model name
AI_MODEL = 'claude-sonnet-5'


# ------------------------------------------------------------------
# LOADING FILES
# ------------------------------------------------------------------

def try_read_csv_with_encodings(csv_path, **kwargs):
    # UTF-8 is the most common encoding, but files exported from Excel with
    # Cyrillic text (Ukrainian, Russian, etc.) are often saved as windows-1251
    # or cp1252 instead — try a few common ones before giving up
    encodings_to_try = ['utf-8', 'windows-1251', 'cp1252', 'latin1']

    for encoding in encodings_to_try:
        try:
            df = pd.read_csv(csv_path, encoding=encoding, **kwargs)
            if encoding != 'utf-8':
                print(f'Note: this file was not UTF-8, read it as {encoding} instead.')
            return df
        except UnicodeDecodeError:
            continue

    # if none of them worked cleanly, fall back to utf-8 with errors replaced,
    # so the tool can still run rather than stopping entirely
    print('Could not confidently detect the file encoding — reading it as UTF-8')
    print('and replacing any unreadable characters. Some text may look incorrect.')
    return pd.read_csv(csv_path, encoding='utf-8', encoding_errors='replace', **kwargs)


def load_dataset(path, sheet_name=None):
    """Load a CSV or Excel file into a DataFrame.

    Handles encoding issues (e.g. Cyrillic text saved as windows-1251),
    delimiter detection (semicolon-separated files), and multi-sheet Excel
    files. Raises FileNotFoundError or ValueError with a clear message
    rather than a raw pandas traceback.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f'Could not find the file: {path}')

    file_extension = os.path.splitext(path)[1].lower()

    if file_extension in ['.xlsx', '.xls']:
        excel_file = pd.ExcelFile(path)
        available_sheets = excel_file.sheet_names

        if sheet_name is None:
            if len(available_sheets) > 1:
                print(f'This Excel file has {len(available_sheets)} sheets: {available_sheets}')
                print(f'No sheet was specified, so using the first one: "{available_sheets[0]}"')
                print('To check a different sheet, pass sheet_name="sheet name".')
            sheet_name = available_sheets[0]
        elif sheet_name not in available_sheets:
            raise ValueError(f'"{sheet_name}" is not a sheet in this file. Available sheets: {available_sheets}')

        return pd.read_excel(excel_file, sheet_name=sheet_name)
    else:
        if file_extension not in ['.csv', '.tsv', '.txt']:
            print(f'Note: "{file_extension}" is not a recognised extension — trying to read it as a CSV anyway.')

        df = try_read_csv_with_encodings(path)

        # if the whole file landed in a single column, the delimiter is probably
        # not a comma (common with European CSVs that use semicolons) — retry
        # with automatic delimiter detection instead of silently continuing
        # with a badly-parsed file
        if df.shape[1] == 1 and any(char in str(df.columns[0]) for char in [';', '\t', '|']):
            print('This file did not look comma-separated, retrying with automatic delimiter detection...')
            df = try_read_csv_with_encodings(path, sep=None, engine='python')
            print(f'Found {df.shape[1]} columns using this method.')

        return df


# ------------------------------------------------------------------
# COLUMN AUTO-DETECTION (used by quality_report)
# ------------------------------------------------------------------

def is_sensitive_column(col_name):
    col_lower = col_name.lower()
    return any(keyword in col_lower for keyword in SENSITIVE_KEYWORDS)


def _get_clean_values(series, disguised_values=None):
    values_to_exclude = disguised_values if disguised_values is not None else DISGUISED_MISSING
    values = series.dropna()
    values = values[~values.astype(str).isin(values_to_exclude)]
    return values


def auto_detect_columns(df, disguised_values=None):
    """Guess column roles (id / date / outlier-check / text-consistency-check).

    These are heuristics, not certainties — quality_report() lets you
    override any of them by passing an explicit list instead of 'auto'.
    """
    total_rows = len(df)
    id_column = None
    date_columns = []
    outlier_columns = []
    text_consistency_columns = []

    for col in df.columns:
        values = _get_clean_values(df[col], disguised_values)
        if len(values) == 0:
            continue

        uniqueness = values.nunique() / total_rows

        if id_column is None and uniqueness > 0.95 and 'id' in col.lower():
            id_column = col
            continue

        numeric_version = pd.to_numeric(values, errors='coerce')
        numeric_rate = numeric_version.notna().mean()

        date_version = pd.to_datetime(values, errors='coerce', format='mixed')
        date_rate = date_version.notna().mean()

        looks_like_a_label = any(
            keyword in col.lower()
            for keyword in ['id', 'phone', 'code', 'year', 'hour', 'zip', 'postcode']
        )

        if date_rate > 0.9 and numeric_rate < 0.9:
            date_columns.append(col)
        elif numeric_rate > 0.9 and not looks_like_a_label:
            outlier_columns.append(col)
        elif numeric_rate < 0.5 and values.nunique() <= 50 and uniqueness < 0.5:
            text_consistency_columns.append(col)

    return {
        'id_column': id_column,
        'date_columns': date_columns,
        'outlier_columns': outlier_columns,
        'text_consistency_columns': text_consistency_columns
    }


# ------------------------------------------------------------------
# DIAGNOSIS: quality_report()
# ------------------------------------------------------------------

def quality_report(df, source_name='dataframe', id_column='auto', cross_checks=None,
                    outlier_columns='auto', text_consistency_columns='auto', date_columns='auto',
                    low_threshold=10.0, mid_threshold=30.0, disguised_missing=None, verbose=True):
    """Run all data quality checks on a DataFrame and return a report dict.

    id_column, outlier_columns, text_consistency_columns, date_columns can
    each be 'auto' (guessed from the data), a list of column names, or None
    to skip that check. cross_checks is a list of dicts like
    {'col_a': 'Quantity', 'col_b': 'Price', 'col_c': 'Total'}.
    disguised_missing overrides the module-level DISGUISED_MISSING list for
    this call only, e.g. disguised_missing=['UNKNOWN', 'TBD', 'N/A'].
    """
    if len(df) == 0:
        raise ValueError('This DataFrame has no rows — there is nothing to check.')

    disguised_values = disguised_missing if disguised_missing is not None else DISGUISED_MISSING

    if cross_checks is None:
        cross_checks = []

    detected = auto_detect_columns(df, disguised_values)
    id_column = detected['id_column'] if id_column == 'auto' else id_column
    date_columns = detected['date_columns'] if date_columns == 'auto' else (date_columns or [])
    outlier_columns = detected['outlier_columns'] if outlier_columns == 'auto' else (outlier_columns or [])
    text_consistency_columns = detected['text_consistency_columns'] if text_consistency_columns == 'auto' else (text_consistency_columns or [])

    if verbose:
        print('Columns used for checks:')
        print('  ID column:', id_column)
        print('  Date columns:', date_columns)
        print('  Outlier columns:', outlier_columns)
        print('  Text consistency columns:', text_consistency_columns)

    report = {
        'source_file': source_name,
        'dataset_shape': list(df.shape),
        'columns': {},
        'duplicate_rows': int(df.duplicated().sum()),
        'duplicate_ids': None,
        'cross_column_checks': []
    }

    total_rows = df.shape[0]
    for col in df.columns:
        real_missing = df[col].isnull().sum()
        disguised_count = df[col].isin(disguised_values).sum()
        total_missing = real_missing + disguised_count
        missing_percent = round((total_missing / total_rows) * 100, 1)

        report['columns'][col] = {
            'real_missing': int(real_missing),
            'disguised_missing': int(disguised_count),
            'total_missing': int(total_missing),
            'missing_percent': float(missing_percent)
        }

    if id_column and id_column in df.columns:
        report['duplicate_ids'] = int(df[id_column].duplicated().sum())

    for check in cross_checks:
        col_a, col_b, col_c = check['col_a'], check['col_b'], check['col_c']
        if col_a not in df.columns or col_b not in df.columns or col_c not in df.columns:
            continue

        a_num = pd.to_numeric(df[col_a], errors='coerce')
        b_num = pd.to_numeric(df[col_b], errors='coerce')
        c_num = pd.to_numeric(df[col_c], errors='coerce')
        expected = a_num * b_num

        valid_rows = a_num.notna() & b_num.notna() & c_num.notna()
        mismatch = valid_rows & (abs(expected - c_num) > 0.01)

        report['cross_column_checks'].append({
            'formula': f'{col_a} x {col_b} = {col_c}',
            'rows_checked': int(valid_rows.sum()),
            'mismatches_found': int(mismatch.sum())
        })

    report['outliers'] = {}
    for col in outlier_columns:
        if col not in df.columns:
            continue
        values = pd.to_numeric(df[col], errors='coerce').dropna()
        if len(values) == 0:
            continue

        q1, q3 = values.quantile(0.25), values.quantile(0.75)
        iqr = q3 - q1
        lower_bound, upper_bound = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        outlier_count = int(((values < lower_bound) | (values > upper_bound)).sum())

        report['outliers'][col] = {
            'lower_bound': round(float(lower_bound), 2),
            'upper_bound': round(float(upper_bound), 2),
            'outlier_count': outlier_count,
            'rows_checked': int(len(values))
        }

    report['text_consistency'] = {}
    for col in text_consistency_columns:
        if col not in df.columns:
            continue

        real_values = df[col].dropna()
        real_values = real_values[~real_values.isin(disguised_values)]

        try:
            real_values = real_values.astype(str) if not pd.api.types.is_string_dtype(real_values) else real_values
            grouped = real_values.groupby(real_values.str.lower())
        except (AttributeError, TypeError):
            if verbose:
                print(f'Skipping text consistency check on "{col}" — it does not look like a text column.')
            continue

        inconsistent_keys = [key for key, group in grouped if group.nunique() > 1]

        if is_sensitive_column(col):
            example_values = []
        else:
            example_values = []
            for key in inconsistent_keys[:3]:
                variants = real_values[real_values.str.lower() == key].unique().tolist()
                example_values.append(' / '.join(variants))

        report['text_consistency'][col] = {
            'inconsistent_value_groups': int(len(inconsistent_keys)),
            'examples': example_values
        }

    report['date_checks'] = {}
    for col in date_columns:
        if col not in df.columns:
            continue

        real_values = df[col].dropna()
        real_values = real_values[~real_values.isin(disguised_values)]

        parsed = pd.to_datetime(real_values, errors='coerce', format='mixed')
        failed_count = int(parsed.isnull().sum())

        shape_counts = real_values.astype(str).str.replace(r'\d', '#', regex=True).value_counts()

        report['date_checks'][col] = {
            'values_checked': int(len(real_values)),
            'failed_to_parse': failed_count,
            'different_formats_found': int(len(shape_counts)),
            'example_formats': shape_counts.head(3).index.tolist()
        }

    report['_thresholds'] = {'low': low_threshold, 'mid': mid_threshold}
    return report


# ------------------------------------------------------------------
# CLEANING FUNCTIONS — each takes a DataFrame and returns a new one.
# For dropping rows with missing values, just use pandas directly:
#   df = df.dropna(subset=['column_name'])
# There is no wrapper for that here — pandas' own version is already a
# one-liner and wrapping it would not add anything.
# ------------------------------------------------------------------

def fill_disguised_missing(df, columns=None, disguised_values=None, verbose=True):
    """Convert placeholder text ("UNKNOWN", "ERROR", etc.) into real NaN.

    Returns a new DataFrame — the original is not changed.
    """
    df = df.copy()
    values_to_treat_as_missing = disguised_values if disguised_values is not None else DISGUISED_MISSING
    columns_to_check = columns if columns is not None else df.columns

    total_converted = 0
    for col in columns_to_check:
        if col not in df.columns:
            continue
        matches = df[col].isin(values_to_treat_as_missing)
        total_converted += int(matches.sum())
        df.loc[matches, col] = pd.NA

    if verbose:
        print(f'Converted {total_converted} disguised missing values to NaN.')

    return df


def fill_missing(df, column, strategy, verbose=True, log=None):
    """Fill missing values in a single column.

    strategy is 'median' (numbers only), 'mode' (most common value), or
    any other value, which is used literally as the fill value.
    Refuses to run 'median' on a non-numeric column, to avoid overwriting
    real data with blanks.

    Pass a dict as log to record what happened under log[column] — used
    by the command-line tool, not needed for normal notebook use.
    """
    df = df.copy()

    if column not in df.columns:
        raise KeyError(f'"{column}" is not a column in this DataFrame.')

    missing_count = int(df[column].isnull().sum())
    if missing_count == 0:
        if verbose:
            print(f'"{column}" has no missing values, nothing to do.')
        return df

    if strategy == 'median':
        non_missing = df[column].dropna()
        numeric_check = pd.to_numeric(non_missing, errors='coerce')
        numeric_rate = numeric_check.notna().mean() if len(non_missing) > 0 else 0

        if numeric_rate < 0.8:
            print(f'Warning: "{column}" does not look like a numeric column ({numeric_rate:.0%} of its values are numbers).')
            print('Skipping the median fill to avoid overwriting real data — returned unchanged.')
            if log is not None:
                log[column] = {'strategy': 'median', 'error': 'column is not numeric, skipped to avoid data loss', 'missing_left_as_is': missing_count}
            return df

        numeric_col = pd.to_numeric(df[column], errors='coerce')
        median_value = numeric_col.median()
        df[column] = numeric_col.fillna(median_value)
        if verbose:
            print(f'Filled {missing_count} missing values in "{column}" with the median ({median_value}).')
        if log is not None:
            log[column] = {'strategy': 'median', 'fill_value': float(median_value), 'values_filled': missing_count}

    elif strategy == 'mode':
        mode_values = df[column].mode(dropna=True)
        if len(mode_values) == 0:
            if verbose:
                print(f'"{column}" has no mode (all values are missing) — returned unchanged.')
            if log is not None:
                log[column] = {'strategy': 'mode', 'note': 'no mode found, left as is', 'missing_left_as_is': missing_count}
            return df
        fill_value = mode_values[0]
        df[column] = df[column].fillna(fill_value)
        if verbose:
            print(f'Filled {missing_count} missing values in "{column}" with the most common value ("{fill_value}").')
        if log is not None:
            log[column] = {'strategy': 'mode', 'fill_value': str(fill_value), 'values_filled': missing_count}

    else:
        df[column] = df[column].fillna(strategy)
        if verbose:
            print(f'Filled {missing_count} missing values in "{column}" with "{strategy}".')
        if log is not None:
            log[column] = {'strategy': 'custom value', 'fill_value': str(strategy), 'values_filled': missing_count}

    return df


def flag_outliers(df, column, verbose=True, log=None):
    """Add a boolean column '{column}_is_outlier' using the IQR method.

    This only flags outliers, it never removes or changes any data —
    a large value might be a genuine order, not an error, so the
    decision of what to do about it is left to you.
    """
    df = df.copy()

    if column not in df.columns:
        raise KeyError(f'"{column}" is not a column in this DataFrame.')

    values = pd.to_numeric(df[column], errors='coerce')
    q1, q3 = values.quantile(0.25), values.quantile(0.75)
    iqr = q3 - q1
    lower_bound, upper_bound = q1 - 1.5 * iqr, q3 + 1.5 * iqr

    flag_column = f'{column}_is_outlier'
    df[flag_column] = (values < lower_bound) | (values > upper_bound)
    flagged = int(df[flag_column].sum())

    if verbose:
        print(f'Flagged {flagged} outliers in "{column}" (valid range: {lower_bound:.2f} to {upper_bound:.2f}). Added column "{flag_column}".')

    if log is not None:
        log[column] = {
            'flag_column_added': flag_column,
            'rows_flagged': flagged,
            'note': 'flagged only, not removed or changed'
        }

    return df


def standardise_casing(df, column, verbose=True, log=None):
    """Make casing consistent within a column, e.g. "cash" and "Cash" both
    become whichever spelling is more common. Returns a new DataFrame.
    """
    df = df.copy()

    if column not in df.columns:
        raise KeyError(f'"{column}" is not a column in this DataFrame.')

    real_values = df[column].dropna()
    if len(real_values) == 0:
        if verbose:
            print(f'"{column}" has no values to standardise.')
        return df

    try:
        real_values = real_values.astype(str) if not pd.api.types.is_string_dtype(real_values) else real_values
        most_common_spelling = real_values.groupby(real_values.str.lower()).agg(
            lambda group: group.value_counts().idxmax()
        )
    except (AttributeError, TypeError):
        print(f'"{column}" does not look like a text column — returned unchanged.')
        return df

    values_changed = 0
    for lowercase_key, correct_spelling in most_common_spelling.items():
        mask = (df[column].astype(str).str.lower() == lowercase_key) & (df[column] != correct_spelling)
        values_changed += int(mask.sum())
        df.loc[mask, column] = correct_spelling

    if verbose:
        print(f'Standardised {values_changed} values in "{column}".')

    if log is not None:
        if values_changed > 0:
            log[column] = {'values_standardised': values_changed}

    return df


def standardise_dates(df, column, date_format='%Y-%m-%d', verbose=True, log=None):
    """Convert a date column to a single consistent format.

    Refuses to run on a column that does not actually look like dates,
    to avoid overwriting real data with blanks.
    """
    df = df.copy()

    if column not in df.columns:
        raise KeyError(f'"{column}" is not a column in this DataFrame.')

    non_missing = df[column].dropna()
    if len(non_missing) == 0:
        if verbose:
            print(f'"{column}" has no values to standardise.')
        return df

    parsed = pd.to_datetime(non_missing, errors='coerce', format='mixed')
    parse_rate = parsed.notna().mean()

    if parse_rate < 0.8:
        print(f'Warning: "{column}" does not look like a date column ({parse_rate:.0%} of its values parse as dates).')
        print('Skipping date formatting to avoid overwriting real data — returned unchanged.')
        if log is not None:
            log[column] = {'error': 'column does not look like dates, skipped to avoid data loss'}
        return df

    full_parsed = pd.to_datetime(df[column], errors='coerce', format='mixed')
    failed_count = int(full_parsed.isnull().sum() - df[column].isnull().sum())

    df[column] = full_parsed.dt.strftime(date_format)

    if verbose:
        print(f'Standardised "{column}" to {date_format} format. {failed_count} values could not be parsed.')

    if log is not None:
        log[column] = {'converted_to': date_format, 'values_that_could_not_be_parsed': failed_count}

    return df


# ------------------------------------------------------------------
# AI SUMMARY
# ------------------------------------------------------------------

def build_fallback_summary(report):
    # a simple rule based summary, used only if the API call fails
    # so the tool still produces a full result without an internet connection
    cols_sorted = sorted(report['columns'].items(), key=lambda item: item[1]['missing_percent'], reverse=True)
    top_three = cols_sorted[:3]

    lines = ['Overview']
    lines.append(f"The dataset contains {report['dataset_shape'][0]} rows and {report['dataset_shape'][1]} columns.")
    lines.append(f"Duplicate rows found: {report['duplicate_rows']}.")

    if report.get('cross_column_checks'):
        total_mismatches = sum(c['mismatches_found'] for c in report['cross_column_checks'])
        lines.append(f"Cross-column mismatches found: {total_mismatches}.")

    lines.append('')
    lines.append('Top issues')
    for col_name, stats in top_three:
        if stats['missing_percent'] > 0:
            lines.append(f"- {col_name}: {stats['missing_percent']}% missing or placeholder values.")

    outlier_lines = [f"- {col}: {stats['outlier_count']} outliers found." for col, stats in report.get('outliers', {}).items() if stats['outlier_count'] > 0]
    if outlier_lines:
        lines.append('- Outliers:')
        lines.extend(outlier_lines)

    text_lines = [f"- {col}: {stats['inconsistent_value_groups']} inconsistent spellings found." for col, stats in report.get('text_consistency', {}).items() if stats['inconsistent_value_groups'] > 0]
    if text_lines:
        lines.append('- Text consistency:')
        lines.extend(text_lines)

    date_lines = []
    for col, stats in report.get('date_checks', {}).items():
        if stats['failed_to_parse'] > 0:
            date_lines.append(f"- {col}: {stats['failed_to_parse']} values could not be read as a date.")
        if stats.get('different_formats_found', 1) > 1:
            date_lines.append(f"- {col}: {stats['different_formats_found']} different date formats mixed together.")
    if date_lines:
        lines.append('- Date format:')
        lines.extend(date_lines)

    lines.append('')
    lines.append('Recommendation')
    if top_three and top_three[0][1]['missing_percent'] > 0:
        lines.append(f"Start by reviewing {top_three[0][0]}, as it has the highest share of missing data.")
    else:
        lines.append('No major issues were found in this dataset.')

    return '\n'.join(lines)


def ai_summary(report, model=None, use_ai=True):
    """Send a quality_report() dict to Claude and get back a plain-language
    summary. Falls back to a simple rule-based summary if no API key or
    connection is available, so this never raises for missing credentials.

    Set use_ai=False, or the environment variable DQP_OFFLINE=1, to skip
    the API call entirely — useful if your organisation's policy does not
    allow sending data (even aggregated numbers) to an external API.
    """
    if not use_ai or os.environ.get('DQP_OFFLINE') == '1':
        return build_fallback_summary(report)

    model = model or AI_MODEL

    system_prompt = """You are a data quality assistant. You write short, plain-English
summaries of data quality reports for junior analysts.

Strict rules:
1. Only use numbers and facts that are present in the JSON report given to you.
2. Never guess or invent reasons why data might be missing or wrong.
3. Keep the tone plain and factual, not dramatic.
4. Use British English spelling.
5. Structure your answer in three parts: Overview, Top issues, Recommendation.
6. Keep it under 200 words total.
7. The report may include outlier counts, text casing inconsistencies, mixed date formats, date parsing failures, and more than one cross-column formula, alongside missing values. Mention these only if they are non-zero."""

    user_prompt = f"Here is a data quality report. Write a summary following the rules above.\n\n{json.dumps(report, indent=2)}"

    try:
        import anthropic
        client = anthropic.Anthropic()

        response = client.messages.create(
            model=model,
            max_tokens=500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        return response.content[0].text

    except Exception as error:
        print('AI summary could not be generated, using a fallback instead.')
        print('Reason:', error)
        if 'model' in str(error).lower():
            print(f'This may be because "{model}" is no longer available. Check docs.claude.com for the current model name.')
        return build_fallback_summary(report)


# ------------------------------------------------------------------
# DASHBOARD
# ------------------------------------------------------------------

def _severity_class(percent, low_threshold, mid_threshold):
    if percent < low_threshold:
        return 'low'
    elif percent < mid_threshold:
        return 'mid'
    else:
        return 'high'


def _build_column_rows_html(report, low_threshold, mid_threshold):
    cols_sorted = sorted(report['columns'].items(), key=lambda item: item[1]['missing_percent'], reverse=True)
    rows_html = ''
    for col_name, stats in cols_sorted:
        severity = _severity_class(stats['missing_percent'], low_threshold, mid_threshold)
        safe_name = html.escape(str(col_name))
        rows_html += f"""
    <div class="col-row">
      <div>
        <div class="col-name">{safe_name}</div>
        <div class="col-detail">{stats['real_missing']:,} blank &middot; {stats['disguised_missing']:,} placeholder</div>
      </div>
      <div class="bar-track"><div class="bar-fill {severity}" style="width:{stats['missing_percent']}%"></div></div>
      <div class="col-pct pct {severity}">{stats['missing_percent']}%</div>
    </div>"""
    return rows_html


def _build_extra_checks_html(report):
    blocks = ''

    if report.get('cross_column_checks'):
        rows = ''
        for check in report['cross_column_checks']:
            severity = 'high' if check['mismatches_found'] > 0 else 'low'
            safe_formula = html.escape(check['formula'])
            rows += f"""
    <div class="col-row">
      <div>
        <div class="col-name">{safe_formula}</div>
        <div class="col-detail">{check['rows_checked']:,} rows checked</div>
      </div>
      <div class="bar-track"><div class="bar-fill {severity}" style="width:{min(check['mismatches_found'] / max(check['rows_checked'],1) * 100 * 5, 100)}%"></div></div>
      <div class="col-pct pct {severity}">{check['mismatches_found']}</div>
    </div>"""
        blocks += f"""
  <section>
    <h2>Cross-column checks</h2>
    <p class="section-sub">Rows where a formula between columns does not add up.</p>
    {rows}
  </section>"""

    if report.get('outliers'):
        rows = ''
        for col, stats in report['outliers'].items():
            severity = 'high' if stats['outlier_count'] > 0 else 'low'
            safe_name = html.escape(str(col))
            rows += f"""
    <div class="col-row">
      <div>
        <div class="col-name">{safe_name}</div>
        <div class="col-detail">valid range: {stats['lower_bound']} to {stats['upper_bound']}</div>
      </div>
      <div class="bar-track"><div class="bar-fill {severity}" style="width:{min(stats['outlier_count'] / max(stats['rows_checked'],1) * 100 * 5, 100)}%"></div></div>
      <div class="col-pct pct {severity}">{stats['outlier_count']}</div>
    </div>"""
        blocks += f"""
  <section>
    <h2>Outliers (IQR method)</h2>
    <p class="section-sub">Values that fall well outside the normal range for that column, based on the interquartile range.</p>
    {rows}
  </section>"""

    if report.get('text_consistency'):
        rows = ''
        for col, stats in report['text_consistency'].items():
            severity = 'high' if stats['inconsistent_value_groups'] > 0 else 'low'
            examples = ', '.join(stats['examples']) if stats['examples'] else 'none found'
            safe_name = html.escape(str(col))
            safe_examples = html.escape(examples)
            rows += f"""
    <div class="col-row">
      <div>
        <div class="col-name">{safe_name}</div>
        <div class="col-detail">examples: {safe_examples}</div>
      </div>
      <div class="bar-track"><div class="bar-fill {severity}" style="width:{min(stats['inconsistent_value_groups'] * 20, 100)}%"></div></div>
      <div class="col-pct pct {severity}">{stats['inconsistent_value_groups']}</div>
    </div>"""
        blocks += f"""
  <section>
    <h2>Text consistency</h2>
    <p class="section-sub">Looks for the same value written with different casing, such as "cash" and "Cash".</p>
    {rows}
  </section>"""

    if report.get('date_checks'):
        rows = ''
        for col, stats in report['date_checks'].items():
            mixed_formats = stats.get('different_formats_found', 1) > 1
            severity = 'high' if (stats['failed_to_parse'] > 0 or mixed_formats) else 'low'
            safe_name = html.escape(str(col))

            detail = f"{stats['values_checked']:,} values checked"
            if mixed_formats:
                examples = ', '.join(stats['example_formats'])
                detail += f" &middot; {stats['different_formats_found']} different formats mixed together ({html.escape(examples)})"

            rows += f"""
    <div class="col-row">
      <div>
        <div class="col-name">{safe_name}</div>
        <div class="col-detail">{detail}</div>
      </div>
      <div class="bar-track"><div class="bar-fill {severity}" style="width:{min(stats['failed_to_parse'] / max(stats['values_checked'],1) * 100 * 5, 100)}%"></div></div>
      <div class="col-pct pct {severity}">{stats['failed_to_parse']}</div>
    </div>"""
        blocks += f"""
  <section>
    <h2>Date format</h2>
    <p class="section-sub">Values that could not be read as a valid date, and columns where more than one date format is mixed together.</p>
    {rows}
  </section>"""

    return blocks


def _build_checks_html(report, mid_threshold):
    checks = report.get('cross_column_checks', [])
    if not checks:
        cross_html = 'not checked'
    else:
        total_mismatches = sum(c['mismatches_found'] for c in checks)
        cross_html = f"{total_mismatches} across {len(checks)} formula{'s' if len(checks) != 1 else ''}"

    dup_id_html = 'not checked' if report['duplicate_ids'] is None else str(report['duplicate_ids'])
    critical_cols = sum(1 for stats in report['columns'].values() if stats['missing_percent'] >= mid_threshold)

    return dup_id_html, cross_html, critical_cols


def generate_dashboard(report, ai_summary_text=None, output_path='dashboard.html'):
    """Render a report dict (from quality_report()) into a single HTML
    dashboard file, adapting to the device's light or dark theme.
    """
    if ai_summary_text is None:
        ai_summary_text = 'No AI summary was generated for this report.'

    thresholds = report.get('_thresholds', {'low': 10.0, 'mid': 30.0})
    low_threshold, mid_threshold = thresholds['low'], thresholds['mid']

    rows_html = _build_column_rows_html(report, low_threshold, mid_threshold)
    dup_id_html, cross_html, critical_cols = _build_checks_html(report, mid_threshold)
    extra_checks_html = _build_extra_checks_html(report)

    status_text = f"{critical_cols} columns critical" if critical_cols > 0 else "no critical columns"
    status_color = "var(--critical)" if critical_cols > 0 else "var(--accent)"

    summary_html = ''
    for block in ai_summary_text.strip().split('\n\n'):
        block = block.strip()
        if block:
            summary_html += f'<p>{html.escape(block)}</p>\n'

    safe_source_file = html.escape(report['source_file'])

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="color-scheme" content="light dark">
<title>Data Quality Passport &mdash; {safe_source_file}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap');

  :root {{
    color-scheme: light dark;
    --paper: #FFFFFF;
    --paper-soft: #FAFAF9;
    --ink: #1B1D1C;
    --ink-soft: #6B7470;
    --line: #E7E5DF;
    --accent: #2B8A86;
    --accent-soft: #EAF5F4;
    --warn: #C1862E;
    --critical: #C15347;
  }}

  @media (prefers-color-scheme: dark) {{
    :root {{
      --paper: #17191A;
      --paper-soft: #1F2222;
      --ink: #EDEDEA;
      --ink-soft: #98A19D;
      --line: #303433;
      --accent: #4FBAB5;
      --accent-soft: #1D2C2B;
      --warn: #D99A45;
      --critical: #D97567;
    }}
  }}

  * {{ box-sizing: border-box; }}
  html {{ background: var(--paper); }}
  body {{ margin: 0; background: var(--paper); color: var(--ink); font-family: 'IBM Plex Sans', sans-serif; line-height: 1.55; }}
  .page {{ max-width: 760px; margin: 0 auto; padding: 64px 24px 96px; }}
  header {{ border-bottom: 1px solid var(--line); padding-bottom: 28px; margin-bottom: 40px; }}
  .kicker {{ font-family: 'IBM Plex Mono', monospace; font-size: 13px; color: var(--accent); }}
  h1 {{ font-size: 30px; font-weight: 600; margin: 10px 0 16px; letter-spacing: -0.01em; word-break: break-word; }}
  .meta-row {{ display: flex; gap: 32px; flex-wrap: wrap; font-family: 'IBM Plex Mono', monospace; font-size: 13px; color: var(--ink-soft); }}
  .meta-row div span {{ display: block; color: var(--ink); font-size: 20px; font-weight: 600; margin-top: 2px; }}
  section {{ margin-bottom: 44px; }}
  h2 {{ font-size: 15px; font-weight: 600; margin: 0 0 4px; }}
  .section-sub {{ font-size: 14px; color: var(--ink-soft); margin: 0 0 20px; max-width: 60ch; }}
  .checks {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--line); border: 1px solid var(--line); }}
  .check {{ background: var(--paper-soft); padding: 18px 20px; }}
  .check .label {{ font-size: 13px; color: var(--ink-soft); }}
  .check .value {{ font-family: 'IBM Plex Mono', monospace; font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .value.pass {{ color: var(--accent); }}
  .col-row {{ display: grid; grid-template-columns: 150px 1fr 60px; align-items: center; gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--line); }}
  .col-row:last-child {{ border-bottom: none; }}
  .col-name {{ font-size: 14px; font-weight: 500; }}
  .col-detail {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--ink-soft); margin-top: 2px; }}
  .bar-track {{ height: 8px; background: var(--line); border-radius: 2px; overflow: hidden; }}
  .bar-fill {{ height: 100%; }}
  .bar-fill.low {{ background: var(--accent); }}
  .bar-fill.mid {{ background: var(--warn); }}
  .bar-fill.high {{ background: var(--critical); }}
  .col-pct {{ font-family: 'IBM Plex Mono', monospace; font-size: 14px; font-weight: 600; text-align: right; }}
  .pct.low {{ color: var(--accent); }}
  .pct.mid {{ color: var(--warn); }}
  .pct.high {{ color: var(--critical); }}
  .summary-box {{ background: var(--accent-soft); border-left: 3px solid var(--accent); padding: 24px 28px; }}
  .summary-box h3 {{ font-size: 13px; font-family: 'IBM Plex Mono', monospace; color: var(--accent); margin: 0 0 12px; font-weight: 600; }}
  .summary-box p {{ font-size: 14px; margin: 0 0 14px; }}
  .summary-box p:last-child {{ margin-bottom: 0; }}
  footer {{ border-top: 1px solid var(--line); padding-top: 20px; margin-top: 56px; font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--ink-soft); display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }}

  @media (max-width: 560px) {{
    .checks {{ grid-template-columns: 1fr; }}
    .col-row {{ grid-template-columns: 100px 1fr 50px; gap: 10px; }}
  }}
</style>
</head>
<body>
<div class="page">
  <header>
    <div class="kicker">DATA QUALITY PASSPORT</div>
    <h1>{safe_source_file}</h1>
    <div class="meta-row">
      <div>Rows<span>{report['dataset_shape'][0]:,}</span></div>
      <div>Columns<span>{report['dataset_shape'][1]}</span></div>
      <div>Overall status<span style="color:{status_color};">{status_text}</span></div>
    </div>
  </header>

  <section>
    <h2>Structural checks</h2>
    <p class="section-sub">Checks that look at the dataset as a whole, before looking column by column.</p>
    <div class="checks">
      <div class="check"><div class="label">Duplicate rows</div><div class="value pass">{report['duplicate_rows']}</div></div>
      <div class="check"><div class="label">Duplicate IDs</div><div class="value pass">{dup_id_html}</div></div>
      <div class="check"><div class="label">Cross-column mismatches</div><div class="value pass">{cross_html}</div></div>
      <div class="check"><div class="label">Columns above {mid_threshold:.0f}% missing</div><div class="value" style="color:var(--critical);">{critical_cols}</div></div>
    </div>
  </section>

  <section>
    <h2>Missing data by column</h2>
    <p class="section-sub">Includes real blanks plus placeholder values such as "UNKNOWN" or "ERROR" &mdash; both count as missing.</p>
    {rows_html}
  </section>
  {extra_checks_html}
  <section>
    <div class="summary-box">
      <h3>AI SUMMARY &mdash; GENERATED FROM THE CHECKS ABOVE</h3>
      {summary_html}
    </div>
  </section>

  <footer>
    <span>Data Quality Passport</span>
    <span>Powered by data_quality_toolkit</span>
  </footer>
</div>
</body>
</html>"""

    with open(output_path, 'w') as f:
        f.write(html_template)
