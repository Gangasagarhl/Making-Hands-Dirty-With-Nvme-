"""One-off generator for src/manifest.json from the master prompt's outline.

Run once; afterwards src/manifest.json is the source of truth and is edited by hand.
"""
import json, re, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROMPT = ROOT / "NVMe_PCIe_10000_Line_Extreme_Master_Prompt.md"
lines = PROMPT.read_text(encoding="utf-8").splitlines()

heads = []  # (lineno, text)
for i, l in enumerate(lines, 1):
    if re.match(r"^#{1,2} (Chapter \d+|PART [IVXL]+|DEEP-DIVE MODULE)", l):
        heads.append((i, l.lstrip("# ").strip()))

def rng(start):
    for j, (ln, _) in enumerate(heads):
        if ln == start:
            end = heads[j + 1][0] - 1 if j + 1 < len(heads) else len(lines)
            return f"{start}-{end}"
    raise KeyError(start)

def find(prefix):
    for ln, t in heads:
        if t.startswith(prefix):
            return ln
    raise KeyError(prefix)

ROMAN = "I II III IV V VI VII VIII IX X XI XII XIII XIV XV XVI XVII XVIII XIX XX XXI".split()
part_titles = {}
for ln, t in heads:
    m = re.match(r"PART ([IVXL]+) — (.*)", t)
    if m:
        part_titles[m.group(1)] = (m.group(2).title().replace("Nvme", "NVMe").replace("Pcie", "PCIe")
                                   .replace("Dma", "DMA").replace("Prp", "PRP").replace("Sgl", "SGL")
                                   .replace("Sata/Sas", "SATA/SAS").replace("2026 NVMe 2.4", "2026 NVMe 2.4"), ln)

chapter_part = {}
cur = None
for ln, t in heads:
    m = re.match(r"PART ([IVXL]+) —", t)
    if m:
        cur = m.group(1)
    m = re.match(r"Chapter (\d+) — (.*)", t)
    if m and int(m.group(1)) <= 71:
        chapter_part.setdefault(cur, []).append((int(m.group(1)), m.group(2), ln))

groups = []
groups.append({"id": "front", "title": "Start Here", "pages": [
    {"id": "index", "title": "The NVMe + PCIe Engineering Journey", "kind": "front", "src": "1-300,3469-3580"},
    {"id": "how-to-use", "title": "How to Use This Academy", "kind": "front", "src": "209-292,2389-2490,3135-3300"},
]})
for r in ROMAN:
    title, pln = part_titles[r]
    pages = []
    for num, t, ln in chapter_part.get(r, []):
        pages.append({"id": f"ch{num:02d}", "title": t, "kind": "chapter", "num": num, "src": rng(ln)})
    if r == "XX":
        pages.append({"id": "nvme-2-4", "title": "2026 NVMe 2.4 Updates", "kind": "part", "src": rng(pln) + ",3339-3360,3646-3702,4645-4659"})
    groups.append({"id": f"part-{r.lower()}", "title": f"Part {r} — {title}", "pages": pages})

def part(r):
    return rng(part_titles[r][1])

groups.append({"id": "practice", "title": "Part XXII–XXV — Labs and Trace Analysis", "pages": [
    {"id": "labs-programming", "title": "Programming Labs", "kind": "part", "src": part("XXII")},
    {"id": "trace-lab", "title": "Trace Analysis Laboratory", "kind": "part", "src": part("XXIII")},
    {"id": "lecroy", "title": "Teledyne LeCroy Analyzer Training", "kind": "part", "src": part("XXIV")},
    {"id": "pex-capture", "title": "PEX / Capture File Training", "kind": "part", "src": part("XXV")},
]})
groups.append({"id": "engineering", "title": "Part XXVI–XXIX — Debugging, Performance, Validation", "pages": [
    {"id": "debug-method", "title": "Debugging Methodology", "kind": "part", "src": part("XXVI")},
    {"id": "failure-library", "title": "Failure Library", "kind": "part", "src": part("XXVII")},
    {"id": "performance", "title": "Performance Engineering", "kind": "part", "src": part("XXVIII")},
    {"id": "validation", "title": "Validation Engineering", "kind": "part", "src": part("XXIX")},
]})
groups.append({"id": "growth", "title": "Part XXX–XXXVI — Specs, Projects, Automation, Seniority", "pages": [
    {"id": "spec-school", "title": "Specification Reading School", "kind": "part", "src": part("XXX")},
    {"id": "reading-plan", "title": "Reading Plan", "kind": "part", "src": part("XXXI")},
    {"id": "lab-environment", "title": "Lab Environment", "kind": "part", "src": part("XXXII")},
    {"id": "projects", "title": "Project-Based Learning", "kind": "part", "src": part("XXXIII")},
    {"id": "auto-trace", "title": "Automated Trace Analysis", "kind": "part", "src": part("XXXIV")},
    {"id": "ai-analysis", "title": "AI + NVMe Protocol Analysis", "kind": "part", "src": part("XXXV")},
    {"id": "senior-thinking", "title": "Senior Engineer Thinking", "kind": "part", "src": part("XXXVI")},
]})
groups.append({"id": "career", "title": "Part XXXVII–XL — Interviews, Capstone, Reports", "pages": [
    {"id": "interviews", "title": "Interview Academy", "kind": "part", "src": part("XXXVII")},
    {"id": "scenario-interviews", "title": "Scenario Interviews", "kind": "part", "src": part("XXXVIII")},
    {"id": "capstone", "title": "Final Capstone", "kind": "part", "src": part("XXXIX")},
    {"id": "bug-reports", "title": "Engineering Bug Report Training", "kind": "part", "src": part("XL")},
]})
groups.append({"id": "tools", "title": "Interactive Tools", "pages": [
    {"id": "tool-tlp-decoder", "title": "TLP Header Decoder", "kind": "tool", "src": "4372-4401,543-574"},
    {"id": "tool-nvme-decoder", "title": "NVMe Command Decoder", "kind": "tool", "src": "4402-4421,922-938"},
    {"id": "tool-queue-sim", "title": "Submission/Completion Queue Simulator", "kind": "tool", "src": "4343-4371,850-919"},
    {"id": "tool-prp", "title": "PRP Visualizer", "kind": "tool", "src": "4422-4442,1038-1055"},
    {"id": "tool-calculators", "title": "Bandwidth, Latency and Little's Law Calculators", "kind": "tool", "src": "4443-4520,1864-1879,2488-2518"},
    {"id": "tool-config-decoder", "title": "Config Space and NVMe Register Decoder", "kind": "tool", "src": "683-727,817-847"},
]})
groups.append({"id": "mastery", "title": "Mastery Engine — Source-Grounded Method", "pages": [
    {"id": "me-truth", "title": "Truth Policy, Sources and Decoding Safety", "kind": "part", "src": "3581-3995"},
    {"id": "me-mastery", "title": "Mastery Loop and Event Reasoning", "kind": "part", "src": "3996-4202"},
    {"id": "me-traces", "title": "Synthetic vs Real Traces, Diffing and Golden Traces", "kind": "part", "src": "4203-4264,5010-5060"},
    {"id": "me-lab-progressions", "title": "Lab Progressions and Simulator Requirements", "kind": "part", "src": "4265-4520"},
    {"id": "me-feature-labs", "title": "Feature Labs: Errors, AER, Power, Reset, SR-IOV, ZNS, Fabrics", "kind": "part", "src": "4521-4659"},
    {"id": "me-spec-reading", "title": "Reading Commands, Registers and State Machines", "kind": "part", "src": "4660-4838"},
    {"id": "me-forensics", "title": "Root Cause, Ambiguity and Forensic Reporting", "kind": "part", "src": "4839-5009,5061-5148"},
    {"id": "me-senior", "title": "Communication, Design Review and Formal Models", "kind": "part", "src": "5149-5446"},
    {"id": "me-practice", "title": "Capacity, Status, Identify, Logs and Cross-Layer Practice", "kind": "part", "src": "5447-5751"},
]})
mods = [(ln, t) for ln, t in heads if t.startswith("DEEP-DIVE MODULE")]
dd = []
for ln, t in mods:
    m = re.match(r"DEEP-DIVE MODULE (\d+) — (.*)", t)
    n = int(m.group(1))
    name = m.group(2)
    fix = {"PCIE": "PCIe", "NVME": "NVMe", "I/O": "I/O", "DMA": "DMA", "LTSSM": "LTSSM", "TLP": "TLP", "DLLP": "DLLP",
           "AER": "AER", "MSI": "MSI", "MSI-X": "MSI-X", "PRP": "PRP", "SGL": "SGL", "ZNS": "ZNS", "SR-IOV": "SR-IOV",
           "BAR": "BAR", "MMIO": "MMIO", "CPU,": "CPU,", "NVME-MI": "NVMe-MI", "NVME/TCP": "NVMe/TCP",
           "NVME/RDMA": "NVMe/RDMA", "PEX/PROPRIETARY": "PEX/Proprietary", "SMART": "SMART", "AI-ASSISTED": "AI-Assisted",
           "2.4": "2.4", "2026": "2026"}
    words = [fix.get(w, w.capitalize() if w not in ("AND", "WITH", "OVER") else w.lower()) for w in name.split()]
    dd.append({"id": f"dd{n:02d}", "title": " ".join(words), "kind": "module", "num": n, "src": rng(ln)})
for k in range(0, 60, 10):
    groups.append({"id": f"modules-{k//10+1}", "title": f"Deep-Dive Modules {k+1:02d}–{k+10:02d}", "pages": dd[k:k+10]})
groups.append({"id": "reference", "title": "Reference and Study Aids", "pages": [
    {"id": "mental-model", "title": "The Final Mental Model", "kind": "appendix", "src": "3361-3434"},
    {"id": "command-vs-packet", "title": "Command vs Packet vs Completion", "kind": "appendix", "src": part("LIV") + "," + part("LV")},
    {"id": "misconceptions", "title": "Common Misconceptions", "kind": "appendix", "src": part("LIII")},
    {"id": "analogy-library", "title": "One Concept, Many Views: Analogy Library", "kind": "appendix", "src": part("LI") + "," + part("LII")},
    {"id": "math", "title": "Mathematics for Storage and Links", "kind": "appendix", "src": part("XLV")},
    {"id": "example-data", "title": "Example Data Sets", "kind": "appendix", "src": part("XLVI")},
    {"id": "reference-matrix", "title": "Final Reference Matrix", "kind": "appendix", "src": part("LVIII")},
    {"id": "learning-map", "title": "Learning Map and Progressive Difficulty", "kind": "appendix", "src": part("L") + "," + part("XLIX")},
    {"id": "learning-system", "title": "Testing, Spaced Review and Teach-Back", "kind": "appendix", "src": part("XLVII") + "," + part("XLVIII") + "," + part("LXII") + "," + part("LXI")},
    {"id": "study-plans", "title": "12-Month and 30-Day Study Plans", "kind": "appendix", "src": part("LIX") + "," + part("LX")},
    {"id": "evidence-debugging", "title": "Evidence-Based Debugging and Spec Safety", "kind": "appendix", "src": part("LVI") + "," + part("LVII")},
    {"id": "final-exam", "title": "Senior Rubric and Final Certification Exam", "kind": "appendix", "src": part("LXIII") + "," + part("LXIV")},
    {"id": "final-project", "title": "Final Project: NVMe PCIe Forensic Investigation Platform", "kind": "appendix", "src": part("LXV")},
    {"id": "glossary", "title": "Glossary", "kind": "appendix", "src": part("XLI")},
    {"id": "references", "title": "References and Further Reading", "kind": "appendix", "src": part("LXXI")},
]})
out = {"title": "The NVMe + PCIe Engineering Journey",
       "baseline": "August 2026 NVMe 2.4 specification set",
       "groups": groups}
(ROOT / "src").mkdir(exist_ok=True)
(ROOT / "src" / "manifest.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print(sum(len(g["pages"]) for g in groups), "pages")
