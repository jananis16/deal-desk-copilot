# Deal Desk Copilot

Turns a deal into an approval decision, a routed exception list, and a written summary for the approver. Policy logic is deterministic. The narrative is model-generated.

## The problem

Most deal desks spend the majority of their time on deals that never needed a human. A clean 8% discount on standard paper sits in the same queue as a 38% multi-year with a security addendum and a termination-for-convenience clause, and both wait on the same person.

The fix is not a faster analyst. It is separating the part of the job that is arithmetic against a policy from the part that is judgment. Once you do that, standard deals stop queueing at all, and the complex ones arrive at an approver already structured, already routed, and with a recommendation attached.

This is a working model of that split.

## What it does

- Prices a deal from a product catalog, including seat, platform, usage, and one-time lines, and computes list TCV, net TCV, ACV, and blended discount
- Places the deal in an approval tier based on discount depth, contract value, and term
- Raises exceptions against policy: product discount ceilings, the absolute discount floor, term limits, payment terms, usage commitment minimums, and ramp shape
- Routes non-standard terms to the right reviewers, and blocks the ones that should not proceed on standard paper at all
- Writes the approval summary an approver actually reads, leading with the recommendation

## Example output

From `examples/output_exception.md`, a 36-month deal at 38% with a regulated buyer:

```
APPROVAL REQUIRED  |  VP Sales + Finance  |  SLA 24h
Net TCV $499,800  |  ACV $166,600  |  discount 37.0%  |  36mo

Exceptions:
  [  REVIEW] Platform (base subscription) discounted 38% against a 35% product ceiling.
  [  REVIEW] Enterprise seat discounted 38% against a 30% product ceiling.
  [  REVIEW] Payment terms net 90 are a working capital decision. Finance approval required.
  [  REVIEW] Custom security addendum: Standard for regulated buyers. Route early,
             it is usually the long pole.
  [  REVIEW] Termination for convenience: Revenue is no longer contracted for the
             full term. Finance needs to know before it lands in forecast.
  [  REVIEW] Price hold beyond term: Caps renewal uplift. Price it in now or lose it later.
  [ADVISORY] Blended discount 37.0% is well above the 12% target for new business.
             Land discount is precedent. It follows the account forever.

Approvers: vp sales, finance director, deal desk, security, legal
```

Compare `examples/output_standard.md`, which auto-approves with no human in the loop, and `examples/output_blocked_mfn.md`, where an MFN clause and uncapped liability stop the deal regardless of its economics.

## How it works

Two layers, deliberately separated.

**`src/evaluate.py` is deterministic.** Same deal plus same config returns the same decision every time, with no model call. Approval logic has to be auditable and reproducible. If a deal was approved at a given tier six months ago, you need to be able to show exactly why, and "the model decided" is not an answer that survives an audit or a diligence process.

**`src/summarize.py` calls a model.** Writing a clear, specific, appropriately-hedged paragraph for a busy VP is a language problem, and that is where a model earns its place. It receives the evaluation output as facts and is instructed not to invent any others. If no API key is set it falls back to a deterministic template, so the tool always runs end to end.

Policy lives in YAML rather than code, because in a real deployment the people who need to change thresholds are in Finance and Sales leadership, not engineering.

## Run it

```bash
pip install -r requirements.txt
cd src
python cli.py                      # all sample deals
python cli.py --deal DD-1042       # one deal
python cli.py --format json        # machine readable
python cli.py --out ../examples    # regenerate the markdown examples
```

Set `ANTHROPIC_API_KEY` for model-written summaries. Without it, everything still runs.

## What I would build next

- CRM integration so evaluation runs on opportunity save rather than on demand, with the decision written back to the record
- Slack approval routing, so approvers act in the place they already are and turnaround is measured rather than estimated
- Cycle time and exception instrumentation: which policies fire most, which approvers are the bottleneck, and which thresholds are miscalibrated because everything trips them
- Precedent lookup, so an approver sees what was decided on comparable deals before deciding this one

## A note on the data

Every deal, product, price, and account name in this repo is invented. The policy thresholds are illustrative and belong to a fictional company. No real customer, employer, or commercial data appears anywhere in this project.
