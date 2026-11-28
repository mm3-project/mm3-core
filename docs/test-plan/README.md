# Test plan

`m0-m8.csv` is the acceptance test plan for milestones 0 to 8 (Core spec 0.58, Module Contract
1.0 rev 17): one test case for every requirement that `docs/spec/milestones.toml` assigns to those
milestones, 473 in all.

Columns:

- **Test ID**: `TC-M<milestone>-<nnn>`, numbered in the order of `milestones.toml`.
- **Requisito**: the requirement id the case tests.
- **Descrizione**, **Esito atteso**: what to do and what must happen.
- **Modalità**: *Automatico (pytest)* (covered by the test suite), *Manuale su specchio/admin*
  (done on the mirror or in the admin UI) or *CI / revisione* (a CI gate, a process or a waiver
  in `milestones.toml`).
- **Milestone**, **Sezione spec**: as in `docs/spec/requirements.toml`.
- **Test automatico esistente**: the first test whose name cites the requirement, with the count
  of the others, or the waiver reason.
- **Esito**: left empty, to be filled in when the plan is run.

The plan is written in Italian. It is a snapshot: when a requirement of these milestones changes
(`changed_in` later than 0.58 or rev 17), review its row.
