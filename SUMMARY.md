# Mibanco Challenge Summary

The document describes **Mibanco’s “De la libreta a la caja” hackathon challenge**: build an AI assistant for Doña Rosa, a small shop owner who records business activity through WhatsApp texts and voice notes.

The assistant must:

- Turn messages into structured transactions: **sales, expenses, credit sales, and repayments**.
- Ask follow-up questions when information is missing, without inventing amounts.
- Handle corrections and remember earlier debts.
- Calculate **weekly cash flow = sales + debt repayments − expenses**. Credit sales affect cash only when collected.
- Estimate a sustainable monthly loan payment in soles and explain the calculation, without promising credit approval.

**Deliverable:** Process 20 hidden messages in order within five minutes and submit a CSV with these exact columns:

```csv
id_mensaje,tipo,producto,cantidad,monto,requiere_confirmacion
```

The kit includes 30 example messages, their correct outputs, and a submission template.

**Scoring (100 points):**

- Transaction accuracy: **40**
- Asking about incomplete information: **20**
- Weekly cash flow: **15**
- Ease of use, including voice and typo tolerance: **15**
- Sustainable payment estimate: **10**

The event lasts **120 minutes**, with building from minute 15–100, hidden-set processing from 100–105, and live judging afterward.

The central priority is **accurate records over convincing answers**: extra transactions and invented amounts reduce the score.
