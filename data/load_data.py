from __future__ import annotations

from pathlib import Path

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "credit_card_billing.duckdb"
CSV_PATH = DATA_DIR / "credit_card_billing_data.csv"
SQL_PATH = DATA_DIR / "credit_card_billing_data.sql"


def ensure_data_files_exist() -> None:
    missing = [str(path) for path in [CSV_PATH, SQL_PATH] if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Missing required data files: {', '.join(missing)}")


def create_table_if_needed(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS credit_card_billing (
            transaction_id VARCHAR,
            transaction_date DATE,
            billing_cycle_month VARCHAR,
            cardholder_name VARCHAR,
            employee_id VARCHAR,
            department VARCHAR,
            cost_center VARCHAR,
            merchant_name VARCHAR,
            merchant_category VARCHAR,
            transaction_amount_usd DOUBLE,
            currency_code VARCHAR,
            payment_status VARCHAR,
            card_type VARCHAR,
            region VARCHAR,
            country VARCHAR
        )
        """
    )


def load_csv_data(conn: duckdb.DuckDBPyConnection) -> int:
    conn.execute(
        """
        DELETE FROM credit_card_billing
        """
    )

    rows = conn.execute(
        """
        COPY credit_card_billing
        FROM ?
        (FORMAT CSV, HEADER TRUE)
        """,
        [str(CSV_PATH)],
    ).fetchone()
    return int(rows[0]) if rows else 0


def validate_loaded_data(conn: duckdb.DuckDBPyConnection) -> dict[str, object]:
    query = """
        SELECT
            COUNT(*) AS row_count,
            MIN(transaction_date) AS min_date,
            MAX(transaction_date) AS max_date
        FROM credit_card_billing
    """
    return conn.execute(query).fetchone()


def load_credit_card_billing_data(db_path: str | Path = DB_PATH) -> dict[str, object]:
    data_path = Path(db_path)
    data_path.parent.mkdir(parents=True, exist_ok=True)

    ensure_data_files_exist()

    conn = duckdb.connect(str(data_path))
    try:
        create_table_if_needed(conn)
        load_csv_data(conn)
        row_count, min_date, max_date = validate_loaded_data(conn)
        conn.commit()
        return {
            "db_path": str(data_path),
            "row_count": int(row_count),
            "min_date": str(min_date),
            "max_date": str(max_date),
        }
    finally:
        conn.close()


def main() -> None:
    results = load_credit_card_billing_data()
    print(f"DuckDB database created at: {results['db_path']}")
    print(f"Loaded {results['row_count']} rows from {CSV_PATH.name}")
    print(f"Date range: {results['min_date']} to {results['max_date']}")


if __name__ == "__main__":
    main()
