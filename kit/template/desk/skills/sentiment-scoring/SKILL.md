---
name: sentiment-scoring
version: 1.0.0
description: >
  Use when the owner or another agent asks how the crowd or the news feels about a stock, whether mentions are unusual, or whether
  the tone of an earnings call changed. Trigger phrases: "how is sentiment on X", "are mentions spiking", "what was the tone of the
  call", "is the crowd too bullish", "sentiment before earnings".
identity: An analyst who turns text readings into daily numbers, dates every reading by the moment it arrived, and says when the evidence is thin or the crowd is on one side.
patterns:
  - Ask the llm service to classify each document into the probability of a positive and of a negative reading, then pass those two numbers per document to stats.sentiment_score.
  - Date every document by the time the system received it (received_at). Drop a document that has no reception time instead of using its publication time.
  - Read the daily score S = P(positive) - P(negative) together with the number of documents behind it, and say when a day rests on a handful of documents.
  - Compare mention volume only with the previous 30 days (stats.mention_volume_zscore) and report the z score with the count that produced it.
  - For an earnings call, take the tone gap (questions minus prepared remarks) and its change against the last 4 calls of the same company (stats.call_tone_pead), and combine it with the standardized earnings surprise only for a hold of 5 to 20 days.
  - For a market sentiment index, report the z score against the previous 52 weekly values and the extreme flag, and state the reading as "extreme", with no direction attached.
avoid:
  - Using the publication time of a document to date it, because the reader could not have known it then.
  - Mapping a company name to a ticker without saying how, or mixing companies that share a name.
  - Calling an extreme of a sentiment index a buy or a sell signal; the contrarian reading is an assumption nobody has tested here.
  - Quoting an accuracy, an information coefficient or a lift for sentiment that no study run in this app produced.
  - Treating one day of high mentions as a trend, or trusting a score built from very few documents or from one coordinated source.
  - Using sentiment as a trade trigger on intraday desks, where it is only an alarm next to put/call, the volatility index and mention spikes.
---

# Sentiment scoring

Sentiment is a reading of text, not a price. Build the number, date it honestly, and say how much text is behind it.

## 1. Daily score

1. Collect the documents of the ticker with their reception time. Keep the source of each one.
2. Send each text to the llm service for the two probabilities (positive, negative). The sum cannot exceed 1.
3. Call `quant_call` with `stats.sentiment_score` and the documents. The result has one score and one document count per day.
4. Report the latest score, the count behind it and the dates covered. A day with very few documents is weak evidence: say so.
5. Read any warning of the result in the answer: dropped documents, probabilities out of range, a reception time before the
   publication time.

## 2. Abnormal mention volume

1. Take the daily document counts from step 1.
2. Call `stats.mention_volume_zscore` with the 30 day window. The baseline is the 30 days before each day, so a spike is never
   part of its own baseline.
3. Report the z score with the count and the baseline it came from. A spike without a change in the score is attention, not mood.

## 3. Tone of an earnings call

1. For each call of the company, score the prepared remarks and the question and answer part separately with the llm service.
2. Call `stats.call_tone_pead` with the calls, the standardized earnings surprise of the latest quarter and a hold of 5 to 20
   days. It returns the tone gap, its change against the last 4 calls, a combined score and whether surprise and tone agree.
3. When they conflict, say so and give no direction. The weights are equal until a backtest sets them.

## 4. Market sentiment indexes

Call `stats.sentiment_index_zscore` with the weekly values of the index. Report the z score and the flag. Do not turn an
extreme into a trade: say that extremes happen and that no result in this app shows what follows them.

## 5. What to say at the end

Give the score, the count, the window and the date basis. Name the sources. Name what is missing (for example call transcripts
that the data service did not return) instead of filling the gap.
