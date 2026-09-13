# 100-customer capacity and cost evidence

Measurements are synthetic, made on 9 September 2026 using an Apple M1 Mac mini with 16 GiB RAM, macOS 26.6.2 (arm64), Python 3.12.11 and a local PostgreSQL 17 cluster. The application test client runs in-process without gunicorn, Caddy, TLS or AWS network latency. These are reproducible engineering measurements, not EC2 capacity certification.

## Workload and targets

[security-load.json](evidence/security-load.json) records 100 customers, 130 sources, 3,926 unique accounts and 2,261,574 initially generated daily Cost rows over 193 days/seven months with three services. Base customer account counts cycle through 2, 15 and 101, with 20 added standalone sources, ten additional payer/shared connections and effective shared-account transfers. The requested initial account count (3,932) differs from unique final accounts after the shared mappings; totals use the actual 3,926. No named real customer fixtures or AWS requests are used.

Targets: customer-scoped p95 under 2 seconds at eight readers, zero cross-customer disclosures/HTTP failures and a collection cycle under six hours. The collector uses fake paginated responses against the real publication code; unit/integration tests separately exercise throttling, retry/backoff, worker lease expiry/restart, duplicate/overlap prevention and old-data preservation. Synthetic clients have zero network latency and cannot prove live AWS quota behavior.

| Measurement | Result |
|---|---:|
| Generation | 311.2 s; 337.8 MiB peak Python allocation |
| 130-source, two-month collection | 100.3 s; 656 pages; 471,120 rows published |
| Per-source collection | mean 0.77 s; p95 2.56 s; maximum 2.83 s |
| Customer-scoped concurrent requests | 8 readers, 80 requests; p50 89.5 ms; p95 207.4 ms; zero isolation/HTTP failures |
| Broad administrator dashboard concurrency | 80 requests; p50 626 ms; p95 9,890 ms; maximum 20,211 ms |
| Budget evaluations | 200 in 14.3 s |
| Queue fairness | 393 scheduled jobs; all 130 sources leased; 3.52 ms per lease |
| Cost table with indexes | approximately 1,038 MiB |

The scoped latency and synthetic six-hour targets passed. **Broad portfolio latency remains a rollout sizing risk**: p95 is about 9.9 seconds in this workload. Measure on the proposed EC2 resources before broad rollout and set an agreed administrator-portfolio target; consider query preaggregation/index tuning or a larger web/database host based on evidence. Do not extrapolate subsecond scoped reads to every report or claim this Mac proves a small EC2 can handle the same concurrency.

## Reproduction

Use an empty, isolated PostgreSQL database, DEBUG mode, and a schema administrator with role-creation privileges for the integration suite. The safety check rejects existing customer data and fewer than 100 customers. For the original seven-month workload:

```sh
python manage.py migrate --noinput
python manage.py security_load --customers 100 --months 7 --services 3 \
  --readers 8 --requests 80 --collect-sources 130 \
  --output docs/evidence/security-load.json
```

`--recheck` repeats collection/report/isolation measurements only against an existing all-synthetic database whose name begins `billing_security_`; it cannot target a production database. The latest local recheck is timestamped in the evidence. CI repeats a one-month/one-service version with 100 customers, 130 sources, four readers and 16 requests on the final branch SHA; it supplements, rather than replaces, the larger local history run. No benchmark starts an AWS collector against real accounts.

## Current planning costs (USD)

Official AWS Mumbai EC2 Linux on-demand pricing checked on 9 September 2026: t3.small **$0.0224/hour**, t3.medium **$0.0448/hour**. The [captured price metadata](evidence/ec2-pricing.json) identifies the official regional pricing endpoint/publication date. At 730 hours, one t3.small is $16.35/month and a t3.medium is $32.70/month. See [AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/).

The proposed extra t3.small collector adds $16.35/month compute plus $3.65/month for one public IPv4 address at $0.005/hour ([AWS VPC pricing](https://aws.amazon.com/vpc/pricing/)). Allow an explicitly estimated $2–4 for its encrypted root storage and $5–10 for incremental backup/log retention: **roughly $27–34/month incremental hosting**. Two continuously running t3.small instances have a $32.70 compute floor plus $7.30 for two public IPs; a planning total of **$50–65/month** includes approximate storage/log/backup allowances. Replacing the web host with t3.medium adds about $16.35/month. Storage/log allowances are estimates, not verified EBS/CloudWatch quotes; confirm region, data size and retention at deployment. Exclude taxes, burst CPU credits, transfer and unusual log growth. No NAT gateway is proposed.

Cost Explorer's primary billing view is **$0.01 per paginated API request** ([AWS Cost Explorer pricing](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/)). At the measured fake-pagination workload, 656 requests cost $6.56/cycle; four cycles/day for 30 days imply **$787.20/month** before historical re-fetches, optional capabilities and interactive cached queries. A preliminary allowance of **$800–1,100/month for API calls** is a workload estimate, not a bill or incurred spend. Actual page counts, customer scope, caching, queries and AWS response size can change it substantially. Custom billing views and optional hourly granularity have different pricing; they are not assumed here. Review API usage meters during an authorized pilot and approve spending limits before broader rollout.
