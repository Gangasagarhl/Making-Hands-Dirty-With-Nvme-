# Page structures

## Chapter pages (ids ch01–ch71)

Use these `<h2>` sections in this order (section ids in brackets). The chapter's own sub-topics from the
master prompt (e.g. "1.1 What is information?" … "1.15 DMA") become `<h3>` sub-sections inside
"Engineering explanation" (or get their own `<h2>` between "Beginner explanation" and "Hardware view"
when there are many of them and they are substantial). Do not type numbers in headings.

1. Why this chapter exists [why] — include **Learning objectives** and **Prerequisites** (as `<h3>`s; prerequisites link backward to earlier pages).
2. The story [story] — `callout kid`, then `callout analogy`, then `callout breaks`.
3. Beginner explanation [beginner]
4. Engineering explanation [engineering] — the sub-topics; the `.ladder` for the central concept; the 16 questions (master prompt §4) for the most important concept, as a table or `dl`.
5. Hardware view [hardware]
6. Specification view [spec] — which document(s) and revision; normative vs informative; then six labelled boxes: What is verified? (`callout verified`), What is derived? (`callout derived`), What is implementation-dependent? (`callout impl`), What is version-dependent? (`callout version`), What must be checked in the specification? (`callout notsaid` or a list), What should never be assumed? (`callout warn`).
7. Protocol view [protocol] — what crosses the interface, direction, request/response, ordering.
8. Trace view [trace] — at least one normal and one abnormal synthetic trace, with reasoning.
9. Linux view [linux] — safe inspection commands (`data-kind="read-only"`), what output can and cannot prove.
10. Code / pseudocode [code]
11. Worked example [worked]
12. Common mistakes [mistakes]
13. Debugging connection [debugging]
14. Senior engineer insight [senior]
15. Lab [lab] — full lab template (see writer brief).
16. Exercises [exercises]
17. Quiz [quiz] — ≥ 8 questions with answers.
18. Interview questions [interview] — beginner → senior, model answers hidden.
19. Teach-back challenge [teachback] — explain to a child, a junior engineer, a senior engineer.
20. Chapter summary [summary] — summary + `callout mastery` checklist.
21. Further reading and next chapter [next] — official documents by name, Linux docs, and the link to the next chapter.

For very foundational early chapters (1–3) the Specification / Protocol / Trace views can be lighter but
must still exist and point forward to where the topic is treated fully.

## Deep-dive module pages (ids dd01–dd60)

Follow the module template from the master prompt exactly, as `<h2>` sections in this order:

1. Orientation [orientation] — one paragraph on what this module deepens, links to the chapter(s) that introduced the topic (`callout prereq`) and where it goes next.
2. Required learning objectives [objectives] — beginner, intermediate, advanced, senior, trace-analysis, debugging, validation objectives.
3. Kid story [kid] — a fresh analogy specific to this module (not the library/teacher one unless continuity helps).
4. Analogy boundary [boundary] — what it represents, what it hides, where it misleads.
5. Vocabulary [vocabulary] — table: term · plain meaning · engineering meaning · relationship to adjacent terms · common confusion.
6. Architecture [architecture] — inputs, outputs, actors, state, memory, registers, interfaces, dependencies (diagram + table).
7. Mechanism [mechanism] — numbered steps; each step: who acts, what changes, where it changes, what evidence exists. Include a sequence diagram and the A–G ladder.
8. Specification discipline [spec] — applicable spec + revision; normative, informative, optional, implementation-dependent, version-dependent behaviour; what the spec does not say.
9. Hardware view [hardware] — must participate, may participate, observable, internal/unobservable.
10. Protocol view [protocol] — messages, transactions, request/response, ordering, completion, errors.
11. Trace view [trace] — four synthetic traces: normal, slow, failed, ambiguous — each with analysis.
12. Linux view [linux] — safe commands, output categories, what each proves and cannot prove. No authoritative-looking invented output.
13. Labs [labs] — beginner, intermediate, advanced, senior forensic lab; each with objective, prerequisites, setup, procedure, observations, questions, expected reasoning, solution (`details.solution`), extension.
14. Debugging [debugging] — at least five failure patterns, each: symptom, possible causes, evidence, first check, second check, disambiguating experiment, likely conclusion, confidence.
15. Interview [interview] — 5 beginner, 5 intermediate, 5 advanced, 5 senior questions, each with a model answer in `details.answer`.
16. Quiz [quiz] — ≥ 8 questions with answers (multiple choice with `data-answer` and open).
17. Teach-back [teachback] — to a child, a junior engineer, a senior engineer (with a model explanation for each in `details.answer`).
18. Mastery gate [mastery] — checklist: explain it, diagram it, identify it in a trace, debug it, distinguish it from neighbouring concepts (name them, with links), cite the applicable source.

Modules go deeper than the chapters, not wider: different traces, harder labs, more failure patterns.
Link to the chapter that introduced the topic instead of re-teaching basics.

## Course-part and reference pages

Use the structure the master prompt gives for that part. Turn every list the prompt gives into real
content (e.g. "Lab 3" becomes a full lab; "Failure library" entries become full failure cases with
symptoms, evidence, first abnormal event, root cause, fix). Use the lab template, trace-lab template and
root-cause template wherever they apply. End with a `callout mastery` checklist and links onward.
