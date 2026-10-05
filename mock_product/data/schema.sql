CREATE TABLE credit_card_billing (
    transaction_id          VARCHAR,    -- unique transaction identifier
    transaction_date        DATE,       -- date the transaction posted
    billing_cycle_month     VARCHAR(6), -- billing cycle, format YYYYMM
    cardholder_name         VARCHAR,    -- name of the cardholder
    employee_id             VARCHAR,    -- employee identifier
    department              VARCHAR,    -- department name (e.g. Sales, Engineering, Finance)
    cost_center             VARCHAR,    -- cost center code
    merchant_name           VARCHAR,    -- name of the merchant
    merchant_category       VARCHAR,    -- category (e.g. Airlines, Hotels, Dining, Office Supplies, Software)
    transaction_amount_usd  DECIMAL,    -- transaction amount in USD
    currency_code           VARCHAR,    -- original transaction currency
    payment_status          VARCHAR,    -- Paid, Pending, Overdue
    card_type               VARCHAR,    -- Travel Card, Purchasing Card, Virtual Card
    region                  VARCHAR,    -- business region (e.g. North America, EMEA, APAC)
    country                 VARCHAR     -- country of transaction
);
