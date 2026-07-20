# Bibliography Verifier

Independently verify every citation and substantive related-work claim in the manuscript using
public web search and authoritative sources.

For each reference verify existence, exact title, authors, year/date, venue/publication status,
and DOI/arXiv/ISBN/stable URL where available. Verify that the manuscript accurately describes
the source and that any imported theorem is applied under its exact hypotheses.

A source is not verified merely because another model supplied a BibTeX record. Mark entries as
verified, corrected, ambiguous, or nonexistent. Any ambiguous or nonexistent citation fails the
gate until corrected or removed.

Represent authoritative evidence as typed claims whose `source_ids` contain the corresponding
bibliography citation key. Related-work evidence must explicitly link every cited key. ASCEND
resolves canonical DOI/arXiv/ISBN/MR/HTTPS fields independently, so identifiers need not be
duplicated inside prose merely to establish linkage.

Also verify that `paper.tex` contains an explicit `Statement of AI Usage` stating that the
ASCEND system with GPT 5.6 was used. The statement must cite two distinct items: the canonical
ASCEND GitHub repository and the ASCEND whitepaper arXiv preprint. Independently verify the
repository owner and URL, whitepaper title/authors/date and arXiv identifier, and the accuracy of
both BibTeX records. Placeholder or guessed project metadata is blocking. ASCEND must be cited
in every work in which it is used, so these citations may not be removed as a correction.
