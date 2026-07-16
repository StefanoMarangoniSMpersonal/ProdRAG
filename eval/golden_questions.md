# RAG Evaluation — Golden Question Set (48 Questions)

Each question is tagged with its **source file**, a **category** label, and the **expected answer**. Categories help you evaluate different retrieval/generation capabilities.

**Category key:**
- `SINGLE_FACT` — One specific retrievable fact
- `NUMERICAL` — Answer is a number, date, or measurement
- `COMPARISON` — Requires info from two or more sections/documents
- `LIST` — Answer is a set of items
- `ABSENCE` — Correct answer is "not stated" / tests hallucination resistance

---

| # | Question | Source File | Category | Expected Answer |
|---|----------|-------------|----------|-----------------|
| 1 | Who is the CEO of Aurelia Robotics? | rag_test_document | SINGLE_FACT | Marta Silveira |
| 2 | What is the battery life of the Sparrow Mini? | rag_test_document | NUMERICAL | 22 hours continuous operation |
| 3 | Why was the Kestrel product line discontinued? | rag_test_document | SINGLE_FACT | Due to a critical hinge defect discovered in 1,200 units |
| 4 | How much did Aurelia Robotics raise in its Series C round, and who led it? | rag_test_document | SINGLE_FACT | $30 million, led by Northbridge Ventures |
| 5 | Which company has a higher max payload on its large warehouse robot — Aurelia Robotics or Halcyon Dynamics? | rag_test_document | COMPARISON | Halcyon Dynamics (Griffin-7 at 500 kg vs. Falcon-9X at 450 kg) |
| 6 | What was the firmware version that caused the Rotterdam incident at Aurelia Robotics? | rag_test_document | NUMERICAL | Version 3.2.1 |
| 7 | Has Cedarwood & Finch ever raised venture capital? | rag_test_document | ABSENCE | No; it has been fully self-financed since 2009 |
| 8 | What material is the Wickham Rocking Chair made from, and what is its lead time? | rag_test_document | SINGLE_FACT | Black walnut; 4-week lead time |
| 9 | Who first described Luminex Syndrome, and at which institution? | corpus_scientific_research | SINGLE_FACT | Dr. Yuna Takahashi at the Bergström Institute in Uppsala, Sweden |
| 10 | What percentage of Luminex patients were initially misdiagnosed with essential tremor? | corpus_scientific_research | NUMERICAL | 38% |
| 11 | What drug is used to treat Luminex Syndrome, and by how much does it reduce episode frequency? | corpus_scientific_research | SINGLE_FACT | Velnoprax; it reduces episode frequency by 61% compared to placebo |
| 12 | What is the name of the novel luciferin variant discovered in the Kelbrin Trench? | corpus_scientific_research | SINGLE_FACT | Kelbrinase |
| 13 | How many new bioluminescent species were catalogued during the Kelbrin Trench expedition? | corpus_scientific_research | NUMERICAL | 17 |
| 14 | At 12 months post-burn, what percentage of bacterial diversity had recovered in the burn center compared to the control site? | corpus_scientific_research | NUMERICAL | 78% |
| 15 | What solar-to-hydrogen conversion efficiency did Dr. Morimoto's team achieve? | corpus_scientific_research | NUMERICAL | 14.2% |
| 16 | What is the population of the Republic of Valdoria? | corpus_government_policy | NUMERICAL | 6.8 million (2024 census) |
| 17 | What is the penalty for a first-offense violation of the DPDSA? | corpus_government_policy | NUMERICAL | 50,000 VKR |
| 18 | Which three companies were fined after the DPDSA grace period expired? | corpus_government_policy | LIST | CloudNet Solutions, DataStream AG, and Chirpr |
| 19 | What percentage of Valdoria's electricity came from renewables at the time the REM was passed in 2021? | corpus_government_policy | NUMERICAL | 43% |
| 20 | What minimum salary is required for a Valdorian Blue Card visa? | corpus_government_policy | NUMERICAL | 180,000 VKR per year (approximately $34,000 USD) |
| 21 | What is the current stable version of the NovaBridge API? | corpus_technical_docs | SINGLE_FACT | v3, released in March 2024 |
| 22 | What is the rate limit (requests per second) for a NovaBridge Professional tier customer? | corpus_technical_docs | NUMERICAL | 100 requests per second |
| 23 | What is ForgeScript? | corpus_technical_docs | SINGLE_FACT | A declarative domain-specific language (DSL) with JSON-like syntax used by NovaBridge's data transformation engine ("Forge") for field mapping, type coercion, conditional logic, and aggregation |
| 24 | How many pre-built connectors does NovaBridge offer, and which is the most popular? | corpus_technical_docs | NUMERICAL | 148 connectors; Salesforce CRM is the most popular (used by 68% of customers) |
| 25 | What happens after 5 consecutive webhook delivery failures on NovaBridge? | corpus_technical_docs | SINGLE_FACT | The webhook is automatically disabled and the organization admin receives an email notification |
| 26 | When was Port Kessler founded, and by whom? | corpus_historical_events | SINGLE_FACT | In 1738, by merchant Henrik Kessler |
| 27 | How many people died in the Great Harbour Fire of 1821? | corpus_historical_events | NUMERICAL | 12 |
| 28 | How long is the Birren Canal, and how many locks does it have? | corpus_historical_events | NUMERICAL | 22 kilometers long with 7 locks |
| 29 | What is the oldest surviving structure in Port Kessler? | corpus_historical_events | SINGLE_FACT | The Kessler Warehouse, built in 1752 |
| 30 | What did Theresa Lund win the Nobel Prize for? | corpus_historical_events | SINGLE_FACT | Chemistry (1978), for discovering the Lund Reaction, a catalytic process for synthesizing ammonia alternatives |
| 31 | What was LMC's sepsis mortality rate before and after implementing the SEDB protocol? | corpus_medical_guidelines | COMPARISON | Before: 19.4%; after (within 18 months): 13.8% |
| 32 | In the LMC Pediatric Asthma Pathway, what oxygen saturation level defines "severe"? | corpus_medical_guidelines | NUMERICAL | Below 90% |
| 33 | What is the surgical site infection rate at Lakeview Medical Center compared to the national average? | corpus_medical_guidelines | COMPARISON | LMC: 1.7%; national average: 2.8% |
| 34 | What is the Briarfield Falls Scale, and what score range indicates high risk? | corpus_medical_guidelines | SINGLE_FACT | A proprietary falls-risk assessment tool at LMC evaluating six factors (age, medications, mobility, cognition, fall history, continence), scored 0–18. A score of 11–18 indicates high risk |
| 35 | In Thornton v. Bellweather Industries, what legal doctrine was established? | corpus_legal_cases | SINGLE_FACT | The "sequential disclosure doctrine" — internal complaints gain Whistleblower Protection Statute protection retroactively when they lead to a qualifying external disclosure |
| 36 | How much was Luminos Energy Corp. ordered to pay in total restitution in the Ashford case? | corpus_legal_cases | NUMERICAL | $18.3 million (including statutory interest) |
| 37 | In Rivera v. Stonewall Academy, why was the student expelled? | corpus_legal_cases | SINGLE_FACT | For organizing a petition signed by 120 students demanding the school add a mental health counselor; the headmaster characterized it as disruptive |
| 38 | What test did the court apply in Patel v. QuickDeliver Inc. to determine employment status? | corpus_legal_cases | SINGLE_FACT | The ABC employment test under Illinois law |
| 39 | What is Thornfield University's endowment? | corpus_education | NUMERICAL | $4.3 billion |
| 40 | What is the undergraduate acceptance rate at Thornfield? | corpus_education | NUMERICAL | 19% |
| 41 | What is the "Thornfield Promise"? | corpus_education | SINGLE_FACT | Families earning below $75,000/year pay no tuition; families earning below $150,000/year pay no more than $15,000/year |
| 42 | Which Thornfield athletic program is the most successful? | corpus_education | SINGLE_FACT | Women's lacrosse, with 5 CAC championships (2015, 2017, 2019, 2022, 2024) |
| 43 | Who holds the CIVL single-season assist record? | corpus_sports_culture | SINGLE_FACT | Elena Vasquez (1,247 assists, set in 2022) |
| 44 | How much did the Montreal Aces' David Okonkwo score in the deciding match of the 2024 CIVL Finals? | corpus_sports_culture | NUMERICAL | 38 kills |
| 45 | Who won the 2024 Harlow Medal at the Millhaven Folk Arts Festival, and for what? | corpus_sports_culture | SINGLE_FACT | Ceramicist Tomoko Ishida, for hand-thrown porcelain vessels inspired by tidal patterns |
| 46 | How much has the Kessler Maritime Heritage Foundation raised toward its Birren Canal wing capital campaign? | corpus_sports_culture | NUMERICAL | $2.1 million out of a $3.5 million goal (as of late 2024) |
| 47 | Comparing the two robotics companies, which one has higher 2024 revenue and by how much? | rag_test_document | COMPARISON | Aurelia Robotics at $55.8M vs. Halcyon Dynamics at $47.6M — a difference of $8.2 million |
| 48 | What was the total amount QuickDeliver was ordered to pay (back wages + civil penalty)? | corpus_legal_cases | NUMERICAL | $25.4 million ($22.4M in back wages/benefits + $3M civil penalty) |

---

## Bonus: Trap / Absence Questions (hallucination resistance)

These questions have **no answer** in the corpus. A good RAG system should say "not found" or equivalent, not hallucinate.

| # | Question | Why It's a Trap |
|---|----------|-----------------|
| T1 | Who is the CTO of Cedarwood & Finch? | No CTO is listed for this company |
| T2 | What color is the Falcon-9X robot? | Color is never mentioned |
| T3 | What year was Halcyon Dynamics' Series C round? | Only a Series B is described |
| T4 | What programming languages does ForgeScript compile to? | ForgeScript is a DSL; compilation targets are never discussed |
| T5 | What is the name of Thornfield University's football coach? | Thornfield does not have a football program |
| T6 | What was Dr. Morimoto's previous institution before Kyoto University? | Never stated in the document |
| T7 | What is the population of Port Kessler's Eastbank district today? | Only historical population figures are given; no current Eastbank figure exists |
| T8 | What was the verdict on QuickDeliver's appeal? | The document states QuickDeliver announced it would appeal, but no outcome is given |
| T9 | How many beds does Lakeview Medical Center's PICU have? | LMC is described as a 620-bed hospital with a PICU, but the PICU bed count is never specified |
| T10 | What is the exchange rate of the Valdorian Krone to the Euro? | Only the USD rate (1 USD ≈ 5.3 VKR) is given; Euro rate is not mentioned |
