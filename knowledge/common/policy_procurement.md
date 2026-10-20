# POL-PR-02 Procurement and autonomous execution (sample)

Suppliers below the minimum compliance score set in the guardrail policy (90% by default) are not used, even when they are cheaper or faster. A supplier must also hold every certification the item requires, such as cold-chain, dangerous goods, controlled substance or halal certification.

Among compliant suppliers, the one with the lowest landed cost is chosen when the site's cover lasts longer than that supplier's total lead time plus the lead-time buffer. Otherwise the fastest compliant supplier is chosen. Landed cost includes the supplier price, freight and, for imports, customs duty.

Orders are placed in full packs. An order whose landed cost is more than the price premium threshold (15% by default) above list price, or that covers more days of demand than the cover limit, needs sign-off.

The platform may execute routine actions automatically up to the daily auto-execution budget. Once the budget is reached, further actions wait for a human approver even if they are otherwise routine.

When no compliant source exists, the shortfall is escalated: the planner reviews alternatives, expedites existing orders or qualifies a new supplier. The platform never relaxes a guardrail to close a gap.
