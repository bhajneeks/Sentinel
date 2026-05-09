# Product Roadmap — 2026

Owner: Hiroshi Tanaka (CTO). Last updated: 2026-01-12.

## Q1 2026 — Reliability and onboarding

| Milestone | Owner | Target | Status |
|-----------|-------|--------|--------|
| Migrate billing from Stripe Charges to PaymentIntents | E. Park | 2026-02-14 | In progress |
| New subscriber onboarding flow (3 steps → 1) | Frontend team | 2026-02-28 | In progress |
| 99.9% uptime SLO on order API | Platform team | 2026-03-31 | Not started |
| Sunset legacy `coffee-shipper-v1` service | M. Rossi | 2026-03-15 | In progress |

## Q2 2026 — Personalization

- **Taste profile v2.** Replace the 5-question quiz with a learned model trained on
  re-order patterns. Lead: K. Yamamoto. Target launch: 2026-05-15.
- **Roast-day scheduling.** Let subscribers pick the day-of-week their bag is
  roasted (currently first-available). Lead: Frontend. Target: 2026-06-01.
- **Decaf expansion.** Add Swiss-water decaf SKUs from 2 origins. Lead: Sourcing.
  Target: 2026-06-30.

## Q3 2026 — Wholesale beta

Open private beta of Aroma Cloud Wholesale — a B2B portal for cafés to order
green beans + roasted-to-order pallets. Lead: P. Raman. Target soft launch:
2026-08-30. Full launch held until Q4 if logistics integration with our 3PL
(FleetSpring) isn't ready.

## Q4 2026 — Internationalization

- EU launch (Netherlands, Germany, France) via partnership with a Rotterdam
  fulfillment center.
- Multi-currency billing (EUR, GBP).
- Localized French + German UI.

## Out of scope for 2026

- Mobile apps (web-only this year)
- Loyalty program redesign (deferred to 2027)
- Self-serve API for third-party integrators
