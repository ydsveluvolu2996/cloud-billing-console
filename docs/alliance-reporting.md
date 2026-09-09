# Alliance reporting tracker

Open **Alliance reporting** in the sidebar. The selected reporting month defaults to the last calendar month. Data uses the existing six-hour AWS collection; this feature needs no new AWS permissions or infrastructure.

## Workbook mapping

| Workbook | Dashboard columns and fields |
| --- | --- |
| MoM Summary, A6:X6 | S.No, Account, Customer Legal Entity (as in Partner Central), AWS Account ID, ACE Opportunity ID, April–March month columns, FY Total, Avg / Month, Reporting Mth, Prior Mth, MoM Var, MoM Var %, Flag |
| Account Detail, B5:B12 | Account Name, Customer Legal Entity, AWS Account ID, ACE Opportunity ID, Reporting Month, Prior Month, Prepared By, Date Shared with Alliance |
| Account Detail, B15:H15 | Service, Reporting Mth, Prior Mth, Var, Var %, Flag, Variance Driver / Internal Commentary |
| Account Detail, B29:H33 | Summary note for alliance team |
| Alliance Handoff Log, B5:O5 | Reporting Month, Account, AWS Account ID, ACE Opportunity ID, Bill Pulled On, Prepared By, Shared With (Alliance), Date Shared, Spend, MoM Var %, APN Marked (Y/N), APN Marked Date, Notes / Blocker, Status |

All three views export CSV, including additional currency and coverage columns to distinguish missing figures from zero. Summary and handoff exports include all filtered rows, independently of pagination. Import account IDs as text in Excel to preserve leading zeros. CSV percentages are percentage points (15 means 15%).

Accounts are identified by the dashboard's onboarded AWS account ID and effective customer ownership. Customer and account filters keep transferred historical spend with its original owner. Each linked account has its own entry; a payer row shows that account's own spend and does not duplicate the organization's total. Legal entity and ACE ID are editable monthly metadata, prefilled from the latest earlier entry. No workbook example rows or unlinked client names are seeded into production.

## Calculations

Costs are stored unblended AWS charges in the selected currency. The April–March financial year follows the selected month. FY Total sums available months, including any imported months after the selected reporting month. Avg / Month divides by months with data, including verified zero months. Coverage labels accompany provisional values; these totals are not assertions of a finalized full-year bill.

An absent account cost row is treated as zero only when the account was owned during the month, its collection source has complete month coverage, and that currency is evidenced in the source's month. Otherwise the value remains missing. Future months are blank. Estimated AWS data and partial coverage are labelled and cannot close an APN entry.

MoM Var = reporting month minus prior month. MoM Var % = difference / prior × 100. Review defaults to an absolute change of at least 15%, including decreases. An increase from zero displays “New from zero” and REVIEW; zero to zero is 0%; unavailable comparisons remain blank. April compares with March of the preceding fiscal year. Service names match AWS data without inferred savings-plan or reservation allocation.

## Monthly handoff workflow

1. Select the month and open an account. Enter its legal entity, ACE opportunity ID, preparer, service comments and summary note.
2. **Record current AWS figures** saves a snapshot with the source import time, capture time and actor. The bill-pulled date defaults to the capture date if blank. Partial or estimated snapshots retain their data state.
3. Enter the alliance recipient and actual shared date. **Save tracking** preserves the recorded figures. Use “Has an unresolved blocker” for an issue; ordinary notes do not set Blocked status.
4. After independently completing the Partner Central entry, record APN marked and its date. Closure requires an ended month, complete non-estimated current-month data, current recorded figures, required identity/handoff fields, valid date order, and no blocker.
5. Later AWS changes to either month's amounts, service mix or coverage flag the record for review. Reopen a closed record, save, then record the new figures. Previous snapshots and annotations remain in the audit log. Changing the display threshold alone is not an AWS revision.

This is internal tracking. The application does not email recipients, create ACE opportunities, or write Partner Central/APN entries. The workbook's process notes are not automated actions.

Operators can edit; readers can view and export. POST operations require CSRF protection and an unchanged record revision. Concurrent creation and edits are serialized; snapshot capture holds the same source lock used by the AWS collector. Migration 0007 adds two tables without rewriting existing customer or billing data.
