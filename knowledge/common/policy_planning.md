# POL-PL-01 Planning parameters: safety stock, demand calendar, late shipments and transport (sample)

Safety stock is calculated, not fixed. For each item and site the buffer is z × √(L × σd² + d² × σL²): z comes from the target service level (95% by default, 99% for critical items with impact 8 or more), σd is the day-to-day variation of demand over the last 28 days with calendar effects removed, d is the forecast daily demand, L is the replenishment lead time of the fastest compliant supplier plus the review period, and σL is that supplier's lead-time spread from its delivery record. Planners can switch back to fixed buffers in the guardrail policy.

The forecast is a demand curve, not a single number. The baseline blends the last 7 and 28 days of demand after dividing out any season, holiday or promotion that was active then; the demand calendar then lifts each future day by the factor of the events that apply to the item and site. Stockout timing, expiry waste and sell-through use the same curve.

Seasons, holidays and promotions are entered in the demand calendar by authorised planners with their dates, demand factor and the items and sites they affect. Overlapping events multiply; the combined factor is capped at 10.

A shipment more than the late-cover window (24 hours by default) past its ETA no longer counts as cover. The deficit it was meant to close reopens and is re-planned from the network or another supplier. A late supplier order can be cancelled by the roles allowed to cancel orders; a transfer already on the road is received instead.

Every lane has a cost per trip and a vehicle capacity. Transfers on the same lane and day share trucks: a transfer rides on a truck already booked when it has room, and only opens a new truck when needed. The cost and the saving against sending each load on its own truck are recorded.
