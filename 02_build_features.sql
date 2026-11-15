-- Build nba.features (one row per HCP) and nba.uplift_train (one row per HCP-week).
-- LEAKAGE GUARD: the monthly panel spans 2025-01..2026-06 but the touch log spans
-- 2026-01-05..2026-06-29. Panel features use months <= '2025-12' ONLY, so no
-- feature ever sees prescribing from the period being predicted.
-- Needs nba.ref_partd_benchmark (run 08_fetch_partd.py then 10_partd_benchmark.py first).
-- Run: bq --project_id=fall26-ails-termproj-nba query --use_legacy_sql=false < 02_build_features.sql

CREATE OR REPLACE TABLE nba.features AS
WITH hist AS (
  SELECT
    hcp_id,
    AVG(market_trx)        AS h_market_trx_mo,
    AVG(glucoren_trx)      AS h_glucoren_trx_mo,
    AVG(glucoren_nbrx)     AS h_glucoren_nbrx_mo,
    SAFE_DIVIDE(SUM(glucoren_trx), NULLIF(SUM(market_trx), 0)) AS h_glucoren_share,
    SAFE_DIVIDE(SUM(competitor_a_trx), NULLIF(SUM(market_trx), 0)) AS h_competitor_share,
    SUM(switch_in) - SUM(switch_out) AS h_net_switch,
    AVG(rep_calls)         AS h_rep_calls_mo,
    AVG(samples)           AS h_samples_mo,
    AVG(emails_sent)       AS h_emails_sent_mo,
    SAFE_DIVIDE(SUM(emails_opened), NULLIF(SUM(emails_sent), 0)) AS h_email_open_rate,
    AVG(web_visits)        AS h_web_visits_mo
  FROM nba.raw_hcp_monthly
  WHERE month <= '2025-12'
  GROUP BY hcp_id
),
aff AS (
  SELECT
    hcp_id,
    AVG(IF(channel = 'rep_call',       affinity, NULL)) AS aff_rep_call,
    AVG(IF(channel = 'remote_call',    affinity, NULL)) AS aff_remote_call,
    AVG(IF(channel = 'approved_email', affinity, NULL)) AS aff_email,
    AVG(IF(channel = 'digital_banner', affinity, NULL)) AS aff_digital,
    AVG(IF(channel = 'webinar',        affinity, NULL)) AS aff_webinar,
    AVG(IF(channel = 'conference',     affinity, NULL)) AS aff_conference
  FROM nba.raw_channel_affinity
  GROUP BY hcp_id
)
SELECT
  m.hcp_id, m.specialty, m.region, m.state, m.practice_setting, m.years_in_practice,
  m.pct_medicare, m.pct_commercial, m.pct_medicaid, m.mean_patient_age,
  m.pct_cvd_comorbid, m.glucoren_reject_rate,
  s.segment, s.growth_group, s.decile, s.email_consent,
  h.* EXCEPT (hcp_id),
  a.* EXCEPT (hcp_id),
  -- Real CMS Part D benchmark (data year 2024) for this HCP's specialty and state; falls back to the
  -- national figure when the state has fewer than 20 real prescribers. Specialty 'Other' has no benchmark.
  COALESCE(bs.p50, bu.p50) AS bm_p50_clms,
  COALESCE(bs.p90, bu.p90) AS bm_p90_clms,
  SAFE_DIVIDE(h.h_market_trx_mo * 12 * m.pct_medicare, COALESCE(bs.p50, bu.p50)) AS hcp_vs_bm
FROM nba.raw_hcp_master m
LEFT JOIN nba.raw_hcp_segments s USING (hcp_id)
LEFT JOIN hist h USING (hcp_id)
LEFT JOIN aff a USING (hcp_id)
LEFT JOIN nba.ref_partd_benchmark bs
  ON bs.specialty_group = m.specialty AND bs.state = m.state AND bs.n_prescribers >= 20
LEFT JOIN nba.ref_partd_benchmark bu
  ON bu.specialty_group = m.specialty AND bu.state = 'US';

-- Training table: one row per HCP-week, with the logged action and outcome.
-- is_randomized = TRUE only for the exploration arm (the rows valid for unbiased uplift).
CREATE OR REPLACE TABLE nba.uplift_train AS
SELECT
  t.hcp_id, t.week, t.arm, (t.arm = 'explore') AS is_randomized,
  t.action, t.propensity,
  t.calls_4w, t.emails_4w, t.digital_4w, t.weeks_since_call, t.last_action,
  t.trailing_nbrx_wk, t.engaged, t.complaint, t.nbrx_2w,
  f.* EXCEPT (hcp_id)
FROM nba.raw_touch_log t
JOIN nba.features f USING (hcp_id);
