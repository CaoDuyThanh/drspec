Design Document: DrSpec (The Agentic Contract Tool)

Goal: Build a Stateful Debugging Assistant that bridges the gap between Code (Runtime) and Intent (Semantics) using Design by Contract (DbC).

1. Core Philosophy: The "Semantic Mirror"

Standard debugging tools (debuggers, loggers) tell you what happened. They cannot tell you what was wrong, because they lack the context of "Right."
Standard AI agents try to "fix" code immediately based on syntax errors. They often break logical invariants because they treat code as text, not systems.

DrSpec builds a Semantic Mirror of the codebase. For every function in the source code, there exists a shadow "Contract" that defines its purpose.

The 3 Pillars of Semantic Debugging

Intent Extraction (Tree of Thoughts):

Problem: Code does not explicitly state "I must not cross the polygon." It just calls shortest_path.

Solution: We must infer the "Why". DrSpec treats code as a suggestion of intent and uses reasoning to extract the Implied Contract.

Contract Formalization (DbC):

Problem: Natural language is fuzzy. "Connect the points" is vague.

Solution: We convert vague intent into rigid JSON constraints: graph.is_connected() == True.

Verification Preparedness (Test Generation):

Problem: A rule is useless if you don't have code to check it.

Solution: We pre-generate executable Python test scripts for every contract, ready for the debugger to use.

The "Semantic Blindness" Challenge (Hybrid Intelligence)

Crucial Caveat: Pure code-reading agents cannot invent domain rules (e.g., "Crosswalks have two sides") if the code itself ignores them. To solve this, DrSpec adopts a Hybrid Model:

Human Seeds: Allows developers to insert high-level "hints" for critical domain logic.

Bug-Driven Learning: The system learns new invariants by analyzing human fixes to bugs, not just reading the buggy code.

Visual Discovery: For spatial/geometric data, the tool generates plots and uses Vision Models to spot "obvious" anomalies (asymmetry, collisions) that are hard to describe in text. The agent decides when visualization is beneficial based on semantic analysis of the data types and variable names.

Confidence Threshold: Contracts with confidence scores below 70% are flagged for human review rather than automatically stored. This prevents hallucinated invariants from polluting the database.

2. Architecture Overview & Technical Stack

The system operates as a Knowledge Builder (background tool) that prepares data for a Debugger Agent (user session).

Module A: The Scanner (Tool Capability)

Role: The "Slicer & Filter" Tool. This is NOT an agent itself, but a high-performance Python function that an Agent calls.

Technical Stack: Python, py-tree-sitter, hashlib.

Logic (The incremental_scan Tool):

Input: A target directory or file path.

Slicing: Uses tree-sitter to parse the file into an AST and extract specific function bodies.

Hashing: Computes SHA-256 of the normalized code (ignoring comments/whitespace).

Filtering: Compares hash vs artifacts.code_hash in DuckDB.

Output: Returns a summary string to the Agent: "Scanned 50 files. Found 3 changed functions. Added 3 jobs to processing_queue."

Module B: The Architect Council (The Reasoning Core)

Role: The "Brain". A Multi-Agent System (Bmad) responsible for hallucination-resistant logic extraction.

Mechanism: Proposer-Critic-Judge Protocol + Human Hints.

Inputs:

Source Code.

Human Hints: Checks for special comments (e.g., """@invariant: Path must not cross polygon""") or a hints.json file.

Git History: Checks previous commits to see if invariants were added/fixed recently.

Agent Roles:

Agent A (The Proposer):

Prompt: "Analyze process_crosswalk. Based on variable names (polygon, skeleton) and function calls (shortest_path), hypothesis the strict rules this function must obey."

Output: Draft Contract (JSON).

Agent B (The Critic):

Prompt: "Review the Draft Contract against the Code. Find 3 scenarios where the code explicitly violates these rules. If the code allows null, but the contract says non-null, the contract is wrong."

Output: Critique List.

Agent C (The Judge):

Prompt: "Synthesize the Debate. If the Critic found valid loopholes, loosen the contract. Assign a Confidence Score (0-100) based on ambiguity."

Output: Final JSON + Reasoning Trace.

Module C: The Verification Generator (The Test Strategist)

Role: The "Engineer". It translates abstract JSON rules into executable Python code.

Why we need this: The "Debugger Agent" (in the future) shouldn't waste time figuring out how to test the code. This module pre-compiles the tests.

Nature of Scripts (Distinction from Unit Tests):

Assertion-Only: Unlike standard Unit Tests (which include Setup, Execution, and Assertion), these scripts contain only the Assertion logic (Predicate Functions).

Portable: They accept (input, output) and return True/False. This allows them to be injected into live debugging sessions on runtime data.

Example:

# Verification Script (Predicate)
def verify_connectivity(g):
    return nx.is_connected(g)

def verify_obstacle_avoidance(result_graph, polygon):
    return all(not polygon.contains(edge) for edge in result_graph.edges)

Action:

Reads the Final JSON Contract from Module B.

Generates a standalone Python function def check_invariant(input, output): ....

Saves scripts to artifacts.verification_script.

Module D: The Visualizer (The Eye)

Role: Visual Anomaly Detection (Agent-Decided).

Why we need this: Some bugs are obvious to the eye but invisible to code logic (e.g., symmetry violations, crossing lines).

Technical Stack: matplotlib / networkx + Vision LLM (GPT-4V / Gemini Pro Vision / Claude).

Activation Strategy: Agent-Decided (Not Automatic)

Rather than rigid type-checking or mandatory annotations, the Judge agent decides when visualization would be beneficial based on:
- Semantic analysis of variable names (e.g., "polygon_coords", "skeleton_points")
- Function context and domain (spatial algorithms, graph operations)
- Contract complexity and confidence level

This approach allows the agent to reason: "This function returns Dict[str, List[float]] but variable names suggest geometric data. Visualization would help verify spatial relationships."

Workflow:

Agent Decision: Judge evaluates if visual analysis would add value for this specific function.

Auto-Plot: If decided yes, module generates a Python script to plot inputs (Red) and outputs (Blue).

Vision Analysis: Sends the plot to a Vision Agent.

Prompt: "Describe the relationship between Red and Blue. Is there symmetry? Do lines cross obstacles? Do two distinct points connect to the same target?"

Feedback Loop: If Vision Agent reports an anomaly ("Both points connect to the right"), this text is fed back to Module B (Architect) to formalize a new contract ("Target A must not equal Target B").

3. Data Storage: The "Shadow Database"

Storage Engine: DuckDB (Serverless, Single-file, Columnar).
Location: .shadow/contracts.db

3.1 Version Control Strategy

Do Not Commit: The .shadow/ directory (including the DuckDB file and generated scripts) should be added to .gitignore.

Artifacts, Not Source: These are treated as Cache Artifacts (like .pyc or build folders).

Rebuild Strategy: The database is designed for fast regeneration rather than migration. When schema changes or major updates occur, simply delete .shadow/ and rebuild. The incremental scan with hash comparison ensures only changed functions are reprocessed, making rebuilds efficient for most use cases.

3.2 Table: artifacts (The Source of Truth)

Stores the semantic metadata for every code unit.

Column

Type

Detailed Description

id

VARCHAR

Primary Key. Format: filepath::function_name.

file_path

VARCHAR

Relative path from project root.

code_hash

VARCHAR

Normalized SHA-256 of the function body.

contract

JSON

Crucial: Stores the DbC rules.

verification_script

VARCHAR

New: The executable Python code to verify the contract.

visualization_script

VARCHAR

New: Code to plot the data (e.g., matplotlib).

reasoning_trace

JSON

Stores the ToT/CoV debate logs (Why did we decide this?).

confidence_score

INTEGER

0-100. < 50 implies "Guessing". > 90 implies "Certainty".

status

VARCHAR

VERIFIED, BROKEN, STALE, or PENDING.

last_scan

TIMESTAMP

Time of last static analysis.

3.3 Table: processing_queue (Task Management)

Manages the workload for the AI agents.

Column

Type

Detailed Description

id

VARCHAR

FK to artifacts.id.

priority

INTEGER

1 (Low) to 10 (High - User requested fix).

reason

VARCHAR

HASH_MISMATCH, DEPENDENCY_CHANGED, MANUAL_RETRY.

attempts

INTEGER

Retry count (to prevent infinite loops on hard functions).

added_at

TIMESTAMP

Used for timeout detection.

3.4 Table: dependencies (The Graph)

Column

Type

Description

source_id

VARCHAR

The Caller.

target_id

VARCHAR

The Callee.

type

VARCHAR

DIRECT_CALL, USAGE (Variable access).

3.5 JSON Schema: The contract Column

This is the heart of the system.

{
  "function_signature": "def process_crosswalk(skeleton: List[Point], poly: Polygon) -> Graph",
  "intent_summary": "Connects isolated skeleton points using shortest path, avoiding the polygon.",
  "invariants": [
    {
      "name": "Connectivity",
      "logic": "nx.is_connected(result_graph)",
      "criticality": "HIGH",
      "on_fail": "error"
    },
    {
      "name": "Obstacle Avoidance",
      "logic": "all(not poly.contains(edge) for edge in result_graph.edges)",
      "criticality": "HIGH",
      "on_fail": "error"
    }
  ],
  "io_examples": [
    { "input": "...", "output": "..." } // Synthetic examples generated by Architect
  ]
}


4. Bmad Implementation Plan: The Agent Team

We require 5 specialized agents in total: 1 Manager ("The Librarian") and 4 Analysts ("The Council").

4.1. Agent 0: The Librarian ("The Preprocessor")

Personality: Efficient, Administrative, Fast.

Role: Manages the Project State. It does not write contracts. It prepares the data.

Tools: incremental_scan(path), check_queue_status(), read_git_diff(commit_hash).

System Prompt:

"You are the Project Librarian.
Your job is to keep the Shadow Database synchronized with the Source Code.
When the user updates code, call incremental_scan to detect changes.
If the tool reports new items in the queue, notify the Council."

Responsibility:

Calls the Tree-sitter tool.

Reports: "Codebase scanned. 3 functions changed. Queue size: 3."

4.2. Agent 1: The Contractor ("The Proposer")

Personality: Draconian, Mathematical, Idealistic.

Role: Reads code and assumes strict logical rules. It ignores "messy" implementation details and focuses on "What should be true."

System Prompt:

"You are a Specification Engineer. Your goal is to define strict contracts.
If a function is named find_shortest_path, you MUST verify the output path is valid.
Do not trust the code—trust the intent of the variable names.
Check for Human Hints (comments starting with @invariant).
Output a JSON Draft Contract."

Responsibility: Generates the initial hypotheses (e.g., "Output list must always be sorted").

4.3. Agent 2: The Critic ("The Devil's Advocate")

Personality: Skeptical, Detail-oriented, Hacker.

Role: Reads the Proposer's contract and tries to disprove it by finding counter-examples in the actual source code.

System Prompt:

"You are a Senior Security Auditor. Review the Draft Contract against the Source Code.
The Contractor claimed 'List is always sorted'.
Look at line 45: data.append(new_item). There is no sort() called after this.
Therefore, the contract is invalid. Report this violation."

Responsibility: preventing false positives. Ensuring the contract matches reality.

4.4. Agent 3: The Judge ("The Archivist")

Personality: Neutral, Organized, Synthesizer.

Role: Weighs the Proposer's Idealism against the Critic's Reality. Decides the final rules, determines if visualization is needed, and formats them for the database.

System Prompt:

"You are the Database Guardian.
Input: A Debate between Contractor and Critic.
Task:

1. If the Critic found a loophole, relax the contract (e.g., change 'Must be sorted' to 'Should ideally be sorted').

2. Assign a Confidence Score (0-100).
   - Scores below 70: Flag as NEEDS_REVIEW (do not auto-store).
   - Scores 70-89: Store with VERIFIED status.
   - Scores 90+: Store with HIGH_CONFIDENCE status.

3. Decide if visualization would help verify this contract:
   - Does the function handle spatial/geometric data (Points, Polygons, Graphs)?
   - Would a visual representation reveal anomalies that logic alone might miss?
   - Consider variable names, not just formal types (e.g., 'polygon_coords' suggests geometry).
   - If YES: Set 'needs_visualization': true in the output.

4. Generate the FINAL valid JSON for DuckDB."

Responsibility: Database integrity, confidence gating, visualization decisions, and producing the final artifacts.

4.5. Agent 4: The Vision Analyst ("The Eye")

Personality: Observational, Intuitive.

Role: Looks at plots generated by Module D and describes anomalies.

System Prompt:

"You are a Geometric Anomaly Detector.
Input: An image of data (Blue = Output, Red = Input).
Task: Describe the relationship. Is it symmetric? Are lines crossing polygons?
If you see something weird, flag it."

Responsibility: Catching "Obvious" bugs that text analysis misses.

4.6 The Execution Loop (Python Driver)

This script bridges the Bmad agents with the DuckDB state.

import duckdb

def run_contract_cycle():
    con = duckdb.connect('.shadow/contracts.db')

    # 0. The Librarian (Preprocessor)
    print("Librarian: Scanning for changes...")
    librarian_agent.call_tool("incremental_scan", path="./src")

    # 1. Fetch next job
    job = con.sql("SELECT * FROM processing_queue ORDER BY priority DESC LIMIT 1").fetchone()
    if not job:
        return "No jobs."

    # 2. Prepare Context
    func_id = job[0]
    code_body = get_code_from_file(func_id)

    # 3. Ignite Bmad Council
    context = f"Analyze this function:\n{code_body}\n"

    # Phase 1: Propose
    draft = agent_contractor.run(context)

    # Phase 2: Critique
    critique = agent_critic.run(f"Draft: {draft}\nCode: {code_body}")

    # Phase 3: Judge & Save (with confidence gating)
    final_json = agent_archivist.run(f"Draft: {draft}\nCritique: {critique}\nSynthesize JSON.")

    # Phase 4: Confidence Gate
    confidence = final_json.get('confidence_score', 0)
    if confidence < 70:
        status = 'NEEDS_REVIEW'
        print(f"⚠️ Low confidence ({confidence}%) - flagged for human review")
    elif confidence >= 90:
        status = 'HIGH_CONFIDENCE'
    else:
        status = 'VERIFIED'

    # Phase 5: Generate Test Script (Module C)
    verify_script = agent_test_gen.run(f"Contract: {final_json}\nGenerate a standalone Python verification function.")

    # Phase 6: Visual Check (Module D - Agent Decided)
    viz_script = None
    if final_json.get('needs_visualization', False):
        viz_script = agent_visualizer.run("Generate matplotlib code to plot this data.")
        # ... execute viz_script ...
        # ... send image to Agent 4 (Vision Analyst) ...
        # ... if anomaly, update contract ...

    # 7. Commit to DB
    con.execute("""
        UPDATE artifacts
        SET contract = ?, verification_script = ?, visualization_script = ?, status = ?
        WHERE id = ?
    """, [final_json, verify_script, viz_script, status, func_id])
    con.execute("DELETE FROM processing_queue WHERE id = ?", [func_id])


5. Future Roadmap & Advanced Features

This section outlines the evolution of DrSpec from a "passive auditor" to an "active safety net."

5.0. Backlog Items

Runtime Validation Phase: Execute verification scripts against existing tests to confirm contracts before storing them. If existing tests violate the contract, the contract is likely wrong. This provides an additional layer of hallucination prevention.

5.1. The "Pipeline Mapper" Visualizer

Goal: Move beyond text logs. Allow developers to see the data flow and where the contract breaks.

Implementation:

Frontend: React Flow or Cytoscape.js.

Data Source: Queries the dependencies table for edges and artifacts table for nodes.

Features:

Traffic Light coloring: Nodes turn Red if status = BROKEN.

Click-to-Inspect: Click a node to see the JSON Contract and Reasoning Trace side-by-side with the code.

Flow Highlighting: Select a variable and watch its path highlight across multiple functions.

5.2. Bug-Driven Learning (The "Auto-Heal" System)

Goal: Immediate mitigation of regression bugs without human intervention.

Problem: Agents often miss domain rules until a bug occurs.

Mechanism:

Monitor Diffs: When a user pushes a fix (e.g., git commit "Fix crosswalk connection"), the Librarian flags the function.

Reverse Engineering: The Council compares Code_Before vs Code_After.

Extraction: "The user added a check for if path.intersects(poly). This is a NEW invariant."

Update: The Contract is updated to include this rule, preventing future regressions.

5.3. Semantic Query Engine (Vector Search)

Goal: "Have we solved this before?"

Implementation:

Use DuckDB's array types to store vector embeddings of the reasoning_trace and contract.

Query: "Find other functions that handle 'Polygon Intersection' or have 'Connectivity' constraints."

Benefit: The Architect Agent learns from previous contracts to generate better new ones (Few-Shot Learning), reducing hallucination rates over time.

5.4. Real-Time IDE Integration (LSP)

Goal: Shift left. Warn the user while typing, not just after running tests.

Implementation:

Build a lightweight VS Code Extension that communicates with the local DuckDB.

Action: When the user types a function call (e.g., connect_points(x)), the extension queries the contract.

Display: A "Ghost Text" or Hover Tooltip: "⚠️ Contract Warning: Ensure 'x' is not empty, as required by the downstream 'thin_skeleton' function."

6. The Workflow: From Builder to Debugger

This clarifies how DrSpec interacts with your daily workflow.

Phase 1: Knowledge Building (The "Builder" Tool)

When: Runs in the background or on-demand when you say "Build Knowledge."

What it does: Scans code, runs the Council (A+B+C+D), and populates contracts.db.

Output: A rich database containing JSON Contracts, Python Verification Scripts, and Visualization Generators.

Phase 2: Active Debugging (The "Consumer" Agent)

When: You open a new chat and say "I have a bug in the crosswalk feature."

The Log Agent: Connects to the running app (via DrTrace) and captures live variable values.

The Contractor Agent:

Queries DuckDB: SELECT verification_script, visualization_script FROM artifacts WHERE id = 'process_crosswalk'.

Executes the verification script. (Result: Pass or Fail).

Executes the visualization script using live data.

Shows you the image: "Here is what the data looked like when it failed. Notice the asymmetry?"

7. Decision Log

This section records key architectural decisions made during the design process.

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-01-06 | Added 70% confidence threshold | Contracts below 70% are flagged for human review to prevent hallucinated invariants from auto-storing |
| 2026-01-06 | Removed Fuzzing module | Property-based testing is a rabbit hole. Core value is contract extraction + verification scripts. Fuzzing can be added later if needed |
| 2026-01-06 | Rebuild instead of schema migrations | Database regeneration is fast enough (incremental scan). Simpler than maintaining migration scripts |
| 2026-01-06 | Agent-decided visualization | Rather than rigid type-checking or annotations, the Judge agent decides when visualization would help based on semantic analysis of variable names and function context |
| 2026-01-06 | Runtime Validation Phase → Backlog | Execute verification scripts against existing tests to confirm contracts. Deferred to post-MVP |