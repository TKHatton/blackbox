# The blocked letter

This is not a mockup. It is the actual letter BLACKBOX's Correspondence agent
wrote and tried to send for case `CASE-CMP-2026-0841`, pulled directly from the
live service's event trace on 2026-08-31. The gateway refused to send it.

Read it once yourself: there is no mention of a diagnosis, an illness, treatment,
or health of any kind. It is refused anyway, because of where its content is
derived from, not because of anything it says.

---

Dear Customer,

We are writing to give you our final decision regarding your complaint about the
fees applied to your account ending in 8214.

We have fully upheld your complaint. We are very sorry that we did not support
you properly when you contacted us on July 9, 2026.

At that time, you shared with us that you were dealing with difficult personal
and financial circumstances. We should have recorded this information
immediately on our systems to prevent further fees from being charged. Because
we failed to do this, arrears management fees were incorrectly applied to your
account.

We understand how stressful and difficult this situation must have been for
you, and we are truly sorry for the extra distress our oversight caused during
such a challenging time.

To help make things right, we have credited a total of 255.01 to your account.
This amount is made up of:
- A full refund of the three arrears management fees, totaling 105.00.
- A compensatory payment of 150.01 for the distress and inconvenience this has
  caused you.

This money has already been transferred to your account.

If you are not satisfied with our response, you have the right to take your
complaint to the Financial Services and Pensions Ombudsman (FSPO) in Ireland.
You can find more information or submit a complaint on their website at
www.fspo.ie, or by contacting them directly.

Thank you for your patience while we investigated this for you. We wish you all
the very best.

Sincerely,

Customer Resolutions Team

---

## Why it was blocked anyway

```
Special category data originating in a restricted jurisdiction would be
transferred to US, a third country with no adequacy basis recorded for this
transfer. The content itself contains no health vocabulary: it is restricted
because of where it is derived from, not because of what it says.

Sources traced:
- Assessment.reasoning (internal file note, never for the customer)
- Intake.vulnerability_indicators: "Customer's CRM360 record shows
  'financial_hardship'. The customer's narrative explicitly mentions a
  cancer diagnosis and reduced pay."

Blocked by rule: special_category_third_country_transfer
Destination: PrintPost, US
```

Nothing here was staged. This is the recorded output of a live Gemini call,
blocked by the disclosure gateway on the deployed service.
