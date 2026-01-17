# DrSpec User Scenario: Complete Workflow

This document describes a realistic end-to-end scenario of how a developer uses DrSpec to build a "semantic mirror" of their codebase and leverage it for debugging.

---

## Background

### The Problem

**Alex** is a senior developer working on a Python/JavaScript web application. The codebase has grown to 500+ functions across multiple modules. When bugs occur, Alex spends hours tracing through code, trying to understand:

- What was this function *supposed* to do?
- What are the expected inputs and outputs?
- What invariants should hold true?

Documentation is sparse, and the original authors have left the company.

### The Solution

Alex discovers **DrSpec** - a tool that helps AI agents build "semantic contracts" for functions. These contracts capture:

- **Intent**: What the function is supposed to do
- **Invariants**: Rules that must always be true
- **I/O Examples**: Sample inputs and expected outputs

Once contracts exist, the **Debugger agent** can use them to quickly identify which invariant is violated when a bug occurs.

---

## Phase 1: Installation & Setup

### Step 1.1: Install DrSpec

Alex installs DrSpec via pip:

```bash
pip install drspec
```

Or via npm (for Node.js environments):

```bash
npm install -g drspec
```

Both methods download a pre-built binary - no Python runtime needed on the user's machine.

### Step 1.2: Verify Installation

```bash
drspec --version
```

Output:
```
0.1.0
```

### Step 1.3: Initialize Project

Alex navigates to the project root and initializes DrSpec:

```bash
cd ~/projects/my-webapp
drspec init
```

Output:
```
✓ DrSpec initialized successfully

Drspec Folder: /home/alex/projects/my-webapp/_drspec
Database: /home/alex/projects/my-webapp/_drspec/contracts.db
Agents Folder: /home/alex/projects/my-webapp/_drspec/agents
Templates Copied:
  - librarian.md
  - proposer.md
  - critic.md
  - judge.md
  - debugger.md
Gitignore Updated: Yes
```

**What happened:**
1. Created `_drspec/` folder in project root
2. Created empty DuckDB database at `_drspec/contracts.db`
3. Copied 5 agent prompt templates to `_drspec/agents/`
4. Added `_drspec/` to `.gitignore`

---

## Phase 2: Code Scanning (Librarian Agent)

### Step 2.1: Activate Librarian Agent

Alex opens their AI assistant (Claude Code, Cursor, etc.), open file agent `_drspec/agents/librarian.md` and type `activate agent` to activates the Librarian (The AI reads the librarian prompt and understands its role).

After activation, Alex inputs:

```
Hey, I just set up DrSpec in my project. Can you help me scan my codebase?
```

### Step 2.2: Initial Scan

The Librarian runs the scan command. Since the Librarian is an AI agent, it can use the cli of drspec library:

```bash
drspec scan
```

Output (JSON - for agent parsing):
```json
{"success":true,"data":{"message":"Scanned 87 file(s), found 523 function(s)","path":"/home/alex/projects/my-webapp","recursive":true,"files_scanned":87,"functions_found":523,"functions_new":523,"functions_changed":0,"functions_unchanged":0,"queue_enabled":true},"error":null}
```

If Alex runs it manually with `--pretty`:
```bash
drspec scan --pretty
```

Output (human-readable):
```
✓ Scanned 87 file(s), found 523 function(s)

Path: /home/alex/projects/my-webapp
Recursive: Yes
Files Scanned: 87
Functions Found: 523
Functions New: 523
Functions Changed: 0
Functions Unchanged: 0
Queue Enabled: Yes
```

**What happened:**
1. Tree-sitter parsed all `.py`, `.js`, `.ts` files
2. Extracted 523 function signatures and bodies
3. Computed normalized SHA-256 hash for each function
4. Stored in `artifacts` table with status `PENDING`
5. Added all 523 to processing queue

### Step 2.3: Check Status

The Librarian shows project status:

```bash
drspec status --pretty
```

Output (human-readable):
```
Artifacts:
  total: 523
  by_status:
    PENDING: 523
    VERIFIED: 0
    NEEDS_REVIEW: 0
    STALE: 0
    BROKEN: 0

Queue:
  total: 523
  PENDING: 523
  PROCESSING: 0
  COMPLETED: 0
  FAILED: 0

Contracts:
  total: 0
  average_confidence: 0.0%

Summary:
  items_needing_attention: 523
  completion_rate: 0.0%
```

> When agents call `drspec status` (without `--pretty`), they receive JSON for parsing.

### Step 2.4: Prioritize Critical Functions

Alex asks the Librarian to prioritize the authentication module:

```bash
drspec scan src/auth/
drspec queue prioritize "src/auth/login.py::authenticate_user" 1
drspec queue prioritize "src/auth/login.py::verify_jwt_token" 1
drspec queue prioritize "src/auth/permissions.py::check_permission" 1
```

### Step 2.5: Librarian Handoff

The Librarian tells Alex:

> "Your codebase has been scanned and **523 functions** are queued for contract analysis. I've prioritized your authentication functions.
>
> To start building contracts, please activate the **Architect Council** agents. They will analyze each function through a debate process:
> 1. **Proposer** - Hypothesizes what the function should do
> 2. **Critic** - Challenges the proposal with edge cases
> 3. **Judge** - Makes final decision and assigns confidence score
>
> Would you like me to explain how to activate them?"

---

## Phase 3: Contract Generation (Architect Council)

### Step 3.1: Activate Proposer Agent

Alex starts a new conversation with the AI, open file agent `_drspec/agents/proposer.md` and types `activate agent` to activates the Proposer (The AI reads the proposer prompt and understands its role)

Then Alex inputs:

```
Let's start building contracts for my authentication functions.
```

### Step 3.2: Get Next Queue Item

The Proposer gets the next function to analyze:

```bash
drspec queue next
```

Output:
```json
{
  "success": true,
  "data": {
    "function_id": "src/auth/login.py::authenticate_user",
    "priority": 1,
    "status": "PROCESSING",
    "reason": "NEW",
    "attempts": 1
  },
  "error": null
}
```

### Step 3.3: Get Source Code

The Proposer retrieves the function:

```bash
drspec source get "src/auth/login.py::authenticate_user"
```

Output:
```json
{
  "success": true,
  "data": {
    "function_id": "src/auth/login.py::authenticate_user",
    "file_path": "src/auth/login.py",
    "function_name": "authenticate_user",
    "signature": "def authenticate_user(username: str, password: str) -> Optional[User]:",
    "body": "def authenticate_user(username: str, password: str) -> Optional[User]:\n    \"\"\"Authenticate a user by username and password.\"\"\"\n    user = User.query.filter_by(username=username).first()\n    if user is None:\n        return None\n    if not verify_password(password, user.password_hash):\n        return None\n    user.last_login = datetime.utcnow()\n    db.session.commit()\n    return user",
    "language": "python",
    "start_line": 15,
    "end_line": 25
  },
  "error": null
}
```

### Step 3.4: Proposer Hypothesizes Contract

The Proposer analyzes the code and proposes:

```
Based on my analysis of `authenticate_user`, I propose:

**Intent Summary:**
Authenticates a user by verifying username/password credentials.
Returns the User object if valid, None if invalid.

**Invariants:**
1. If user not found → returns None (criticality: HIGH)
2. If password invalid → returns None (criticality: HIGH)
3. If authenticated → user.last_login is updated (criticality: MEDIUM)
4. If authenticated → returns non-None User (criticality: HIGH)

**I/O Examples:**
- authenticate_user("valid_user", "correct_pass") → User(id=1, username="valid_user")
- authenticate_user("unknown", "any") → None
- authenticate_user("valid_user", "wrong_pass") → None
```

### Step 3.5: Critic Reviews Proposal

Alex activates the Critic agent:

```
Now I need you to act as the Critic agent.
Read _drspec/agents/critic.md for your instructions.
Please review this proposed contract for authenticate_user.
```

The Critic challenges:

```
I found potential issues with the proposed contract:

1. **Missing Invariant**: What if username or password is None/empty?
   - The code doesn't validate inputs
   - Should we add: "If username is empty → returns None or raises ValueError"?

2. **Side Effect Not Captured**: The function commits to database
   - Invariant 3 mentions update but not the commit
   - If commit fails, what happens?

3. **Security Concern**: No rate limiting mentioned
   - Is there an invariant about max attempts?

4. **Edge Case**: What if two requests authenticate same user simultaneously?
   - Race condition on last_login update

Recommend: Confidence should be 65% (NEEDS_REVIEW) until these are addressed.
```

### Step 3.6: Judge Makes Final Decision

Alex activates the Judge agent:

```
Now act as the Judge agent.
Read _drspec/agents/judge.md for your instructions.
Review the Proposer's contract and Critic's feedback.
```

The Judge decides:

```
After reviewing both perspectives, I rule:

**Final Contract:**
{
  "function_signature": "def authenticate_user(username: str, password: str) -> Optional[User]",
  "intent_summary": "Authenticates user by verifying credentials against database. Updates last_login timestamp on success. Returns User if valid, None if invalid.",
  "invariants": [
    {
      "name": "null_on_unknown_user",
      "logic": "User.query.filter_by(username).first() is None → return None",
      "criticality": "HIGH",
      "on_fail": "error"
    },
    {
      "name": "null_on_bad_password",
      "logic": "verify_password(password, hash) is False → return None",
      "criticality": "HIGH",
      "on_fail": "error"
    },
    {
      "name": "updates_last_login",
      "logic": "on success → user.last_login is set to current time",
      "criticality": "MEDIUM",
      "on_fail": "warn"
    },
    {
      "name": "commits_on_success",
      "logic": "on success → db.session.commit() is called",
      "criticality": "MEDIUM",
      "on_fail": "warn"
    }
  ],
  "io_examples": [
    {"input": {"username": "alice", "password": "correct"}, "output": "User(alice)"},
    {"input": {"username": "unknown", "password": "any"}, "output": "None"},
    {"input": {"username": "alice", "password": "wrong"}, "output": "None"}
  ]
}

**Confidence Score:** 72% (VERIFIED)
- Core authentication logic is clear
- Input validation concern is valid but outside function scope
- Rate limiting is handled elsewhere (middleware)

**Reasoning:** The function's primary contract is authentication, which is well-defined.
The Critic's concerns about input validation and rate limiting are architectural
concerns that belong in calling code, not this function's contract.
```

### Step 3.7: Save Contract

The Judge saves the contract:

```bash
echo '{"function_signature": "def authenticate_user...", ...}' | drspec contract save "src/auth/login.py::authenticate_user" --confidence 0.72
```

Output:
```json
{
  "success": true,
  "data": {
    "message": "Contract saved successfully",
    "function_id": "src/auth/login.py::authenticate_user",
    "confidence_score": 0.72,
    "status": "VERIFIED"
  },
  "error": null
}
```

### Step 3.8: Continue with Queue

The Architect Council continues processing the queue:

```bash
drspec queue next
# ... repeat process for each function
```

---

## Phase 4: Ongoing Development

### Step 4.1: Code Changes Detected

Weeks later, Alex modifies `authenticate_user` to add 2FA support:

```python
def authenticate_user(username: str, password: str, totp_code: str = None) -> Optional[User]:
    # ... modified implementation
```

### Step 4.2: Incremental Scan

The Librarian re-scans:

```bash
drspec scan src/auth/login.py
```

Output:
```json
{
  "success": true,
  "data": {
    "message": "Scanned 1 file(s), found 5 function(s)",
    "functions_new": 0,
    "functions_changed": 1,
    "functions_unchanged": 4,
    "queue_enabled": true
  },
  "error": null
}
```

**What happened:**
1. Hash for `authenticate_user` changed (signature modified)
2. Status changed from `VERIFIED` → `STALE`
3. Function re-added to queue with reason `HASH_MISMATCH`

### Step 4.3: Update Contract

The Architect Council updates the contract to reflect the 2FA change.

---

## Phase 5: Debugging with Contracts

### Evidence-Based Debugging

The Debugger agent is most powerful when combined with **external runtime evidence** - this can come from:
- A companion agent like DrTrace (queries logs programmatically)
- User-pasted log snippets
- Stack traces
- Monitoring tool screenshots
- Manual observations

**Collaboration Pattern:**
```
┌─────────────────────────────────────────────────────────────────┐
│                    SAME CHAT SESSION                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Debugger Agent              External Evidence                  │
│  (Master of Source Code)     (Logs, Traces, Observations)       │
│         │                           │                           │
│         │   "What errors do you     │                           │
│         │    see in the logs?"      │                           │
│         │ ──────────────────────────→                           │
│         │                           │                           │
│         │   "NullPointerException   │                           │
│         │    at line 42, happened   │                           │
│         │    15 times in last hour" │                           │
│         │ ←──────────────────────────                           │
│         │                           │                           │
│         │   Contract says line 42   │                           │
│         │   should never be null... │                           │
│         │   "Show me the call       │                           │
│         │    patterns"              │                           │
│         │ ──────────────────────────→                           │
│         │                           │                           │
│         │   "80% of errors come     │                           │
│         │    from path A→B→C"       │                           │
│         │ ←──────────────────────────                           │
│         │                           │                           │
│         ↓                           │                           │
│   ROOT CAUSE: Path A has bug        │                           │
│   FIX: Check null at A entry        │                           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Why Evidence Matters:**

| Source | Knows | Doesn't Know |
|--------|-------|--------------|
| **Debugger** | Source code, contracts, invariants, dependencies | What actually happened at runtime |
| **Evidence** | Logs, traces, frequencies, patterns, actual data | Why the code is written that way |

**Together:** They can reason from evidence to root cause, not just surface symptoms.

**Analogy - The Flat Tire Problem:**
```
Problem: Car has flat tire frequently

Without evidence:
  - "Tire is flat again. Suggest: Replace tire."

With evidence:
  - Evidence: "Flat tire happened 10 times. 8 times after driving road A."
  - Debugger: "Car can take road A, B, or C. Contract says 'avoid rough roads'."
  - Combined insight: "Road A is the root cause. Use road B or C instead."
```

**Note:** In this example, we use DrTrace as the evidence source. If DrTrace is not available, the user can provide evidence manually (paste logs, describe what happened, etc.).

### Step 5.1: Bug Report

A user reports: "Login fails even with correct credentials"

### Step 5.2: Activate Both Agents

Alex activates both the Debugger and DrTrace in the same chat session:

```
I need help debugging a login issue.
Read _drspec/agents/debugger.md and also activate DrTrace.
A user reports login is failing with correct credentials.
```

### Step 5.3: Query Relevant Contracts

The Debugger retrieves the contract:

```bash
drspec contract get "src/auth/login.py::authenticate_user"
```

Output includes the contract with invariants.

### Step 5.4: DrTrace Provides Runtime Evidence

Instead of Alex manually providing runtime data, **DrTrace queries the logs**:

```
[DrTrace]: Let me check the application logs for login failures...

Query: login failures in last 24 hours for user "alice"

Results:
- 15 failed login attempts for "alice"
- All returned None from authenticate_user
- verify_password() returned False in all cases
- password_hash format: "$argon2id$..." (new format)
- But stored hash was: "$2b$..." (bcrypt format)
- Pattern: 100% of failures are users created before 2024-01-01
```

### Step 5.5: Debugger Analyzes with Evidence

The Debugger combines contracts with DrTrace's evidence:

```
[Debugger]: Checking contract invariants against DrTrace evidence:

1. null_on_unknown_user: User EXISTS in database ✓
2. null_on_bad_password: verify_password returned FALSE ✗
   → But DrTrace shows password WAS correct!
3. updates_last_login: last_login was NOT updated ✗
4. commits_on_success: commit was NOT called ✗

**DrTrace Pattern Analysis:**
- 100% of failures are old users (created before 2024-01-01)
- Old users have bcrypt hashes ($2b$...)
- New users have argon2 hashes ($argon2id$...)

**Root Cause Identified:**
The verify_password function now only supports argon2, but old users
have bcrypt hashes. This is a migration issue, not a code bug.

**Why DrTrace Was Critical:**
Without runtime evidence, I might have suggested "check password input"
or "debug verify_password logic". But DrTrace showed the PATTERN:
only old users fail. That's the key insight.

**Fix Options:**
1. Add bcrypt fallback to verify_password (quick fix)
2. Run migration to re-hash all passwords to argon2 (proper fix)
3. Force password reset for affected users (user impact)
```

### Step 5.6: Get Dependencies

The Debugger checks related functions:

```bash
drspec deps get "src/auth/login.py::authenticate_user"
```

Output:
```json
{
  "success": true,
  "data": {
    "function_id": "src/auth/login.py::authenticate_user",
    "calls": [
      "src/auth/crypto.py::verify_password",
      "src/models/user.py::User.query"
    ],
    "called_by": [
      "src/api/auth_routes.py::login_endpoint",
      "src/api/auth_routes.py::refresh_token"
    ]
  },
  "error": null
}
```

### Step 5.7: Check verify_password Contract

```bash
drspec contract get "src/auth/crypto.py::verify_password"
```

The Debugger finds:

```
Contract for verify_password shows:
- Invariant: "returns True if password matches hash using bcrypt"

**Finding:** Recent dependency update changed bcrypt to argon2.
The verify_password contract is now STALE (hash changed).

**Root Cause Confirmed:**
Password hashing algorithm mismatch between stored hashes (bcrypt)
and current verification (argon2).

**Fix:** Add migration to re-hash passwords or support both algorithms.
```

---

## Phase 6: Visualization (Optional)

### Step 6.1: Judge Requests Visualization

During contract generation, the Judge may decide a visualization would help:

```
For this complex algorithm, I need to see the state transitions.
Please generate a flow diagram.
```

### Step 6.2: Generate Plot

```bash
drspec verify run "src/algorithms/state_machine.py::process_event" --visualize
```

This generates a matplotlib/networkx diagram showing:
- State transitions
- Edge conditions
- Invariant check points

### Step 6.3: Vision Analysis

The Judge (or a Vision Analyst agent) examines the plot for:
- Unreachable states
- Missing transitions
- Cycles that violate invariants

---

## Summary: Complete Workflow

```
┌─────────────────────────────────────────────────────────────────┐
│                        USER WORKFLOW                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. INSTALL & INIT                                              │
│     pip install drspec                                          │
│     drspec init                                                 │
│                                                                  │
│  2. SCAN (Librarian Agent)                                      │
│     drspec scan                                                 │
│     drspec status                                               │
│     drspec queue prioritize <id> <priority>                     │
│                                                                  │
│  3. BUILD CONTRACTS (Architect Council)                         │
│     ┌─────────────┐                                             │
│     │  Proposer   │ ──→ Hypothesize contract                    │
│     └──────┬──────┘                                             │
│            ↓                                                     │
│     ┌─────────────┐                                             │
│     │   Critic    │ ──→ Challenge with edge cases               │
│     └──────┬──────┘                                             │
│            ↓                                                     │
│     ┌─────────────┐                                             │
│     │   Judge     │ ──→ Final decision + confidence             │
│     └──────┬──────┘                                             │
│            ↓                                                     │
│     drspec contract save <id>                                   │
│                                                                  │
│  4. ONGOING DEVELOPMENT                                         │
│     drspec scan (detects changes → STALE status)                │
│     Architect Council updates contracts                          │
│                                                                  │
│  5. DEBUG WITH CONTRACTS (Debugger Agent)                       │
│     drspec contract get <id>                                    │
│     drspec deps get <id>                                        │
│     drspec verify run <id>                                      │
│     → Identify violated invariants                              │
│     → Trace to root cause                                       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Key Concepts Recap

| Concept | Description |
|---------|-------------|
| **Semantic Mirror** | Collection of contracts that reflect what code *should* do |
| **Contract** | Intent + Invariants + I/O Examples for a function |
| **Invariant** | A rule that must always be true (e.g., "if user not found → return None") |
| **Confidence Score** | 0-100% certainty in the contract (≥70% = VERIFIED) |
| **Status Values** | PENDING → VERIFIED/NEEDS_REVIEW → STALE → BROKEN |
| **Architect Council** | Proposer + Critic + Judge agents for contract generation |
| **Function ID** | `filepath::function_name` format (e.g., `src/auth/login.py::authenticate`) |

---

## CLI Command Reference

| Command | Purpose | Agent |
|---------|---------|-------|
| `drspec init` | Initialize project | Human |
| `drspec scan [path]` | Discover functions | Librarian |
| `drspec status` | Show project stats | Librarian |
| `drspec queue peek` | Preview queue | Librarian |
| `drspec queue next` | Get next item | Architect Council |
| `drspec queue prioritize <id> <priority>` | Set priority | Librarian |
| `drspec source get <id>` | Get function code | Architect Council |
| `drspec contract get <id>` | Get contract | Debugger |
| `drspec contract save <id>` | Save contract | Judge |
| `drspec contract list` | List contracts | Librarian |
| `drspec deps get <id>` | Get dependencies | Debugger |
| `drspec verify run <id>` | Run verification | Debugger |

---

## Frequently Asked Questions (Design Decisions)

### Q: Is the Proposer → Critic → Judge flow one-way or iterative?

**A: One-way by design (v1).**

The current flow is: `Proposer → Critic → Judge → Done`

**Rationale:**
1. **Simplicity** - Easier to implement, debug, and understand
2. **Cost Control** - Each round costs API tokens; unlimited iterations could be expensive
3. **Convergence Risk** - Iterative debates can sometimes oscillate without reaching consensus
4. **Judge Authority** - The Judge makes the final call; that's its purpose

**How iteration happens (human-in-loop):**
```
Round 1: Proposer → Critic → Judge → NEEDS_REVIEW (low confidence)
         ↓
Human reviews output, decides "try again with critic's feedback"
         ↓
Round 2: Proposer (reads previous critique from DB) → Critic → Judge → VERIFIED
```

The database stores history, so Proposer can see previous critiques when regenerating.

**Future consideration:** Auto-iteration with max rounds (2-3) could be added as an enhancement if manual re-runs become tedious. The database schema already supports versioning.

---

### Q: Will agents know where to start if activated mid-workflow?

**A: Yes, agents detect state via database.**

When you tell an agent "work on function X", it checks:
- `drspec contract get <id>` - Does a contract exist?
- `drspec queue get <id>` - What's the queue status?

| Scenario | What Agent Sees | Agent Response |
|----------|-----------------|----------------|
| No contract exists | `CONTRACT_NOT_FOUND` | Proposer: "I'll create initial contract" |
| Contract exists, no critique | Contract with empty critique | Critic: "I see Proposer's draft, I'll review" |
| Contract has critique | Contract with critique field | Judge: "I see proposal + critique, I'll decide" |
| Critic asked first, no contract | `CONTRACT_NOT_FOUND` | Critic: "No contract yet. Run Proposer first." |

**Same session recommended** for context continuity, but cross-session works because database is the source of truth.

---

### Q: Can Proposer, Critic, and Judge work in different chat sessions?

**A: Yes, but same session is recommended.**

**Same session (recommended):**
- Context flows naturally between agents
- No need to re-explain the function
- More efficient token usage

**Different sessions (supported):**
- Each agent reads state from database
- Works for async workflows or different team members
- Slightly more overhead (agent must re-read context)

The database serves as the coordination mechanism for cross-session work.

---

### Q: Can all three agents (Proposer, Critic, Judge) work together without interruption?

**A: Yes! This is called "Party Mode" - any council agent can offer it.**

Party Mode is a **chat session feature** built into each Architect Council agent. When you activate any of them (Proposer, Critic, or Judge), they can offer to run Party Mode.

**Two workflow options:**

**Manual Mode (step-by-step control):**
```
User activates Proposer → Proposer works → User reviews
User activates Critic → Critic works → User reviews
User activates Judge → Judge works → User reviews final
```

**Party Mode (autonomous):**
```
User activates Proposer → Proposer offers Party Mode → User accepts
→ Proposer reads critic.md and judge.md
→ All 3 personas work in same session → User reviews final result only
```

**How Party Mode works:**
1. User opens any council agent (e.g., `_drspec/agents/proposer.md`) and types "activate agent"
2. User says: "Generate contract for `src/auth.py::login`"
3. Agent asks: "Would you like me to run Party Mode? I'll activate Critic and Judge in this session."
4. User accepts
5. Agent reads the other agent prompt files (`critic.md`, `judge.md`)
6. Agent switches personas: Proposer → Critic → Judge
7. Each persona's output feeds into the next (no human review between steps)
8. If confidence < threshold, can request one revision round (max 2 rounds)
9. User only reviews the final Judge decision

**Benefits:**
- Less interruption for the user
- Faster contract generation for bulk processing
- Natural debate flow preserved
- All work in single chat session (no context loss)
- All intermediate results stored in DB for audit

**When to use which mode:**

| Scenario | Recommended Mode |
|----------|------------------|
| Learning how DrSpec works | Manual |
| Complex/critical functions | Manual |
| Bulk processing many functions | Party Mode |
| Quick iteration | Party Mode |

---

### Q: How does the Debugger work with external evidence (logs, traces, etc.)?

**A: The Debugger combines code knowledge with runtime evidence for root cause analysis.**

The Debugger is designed to work with **any external evidence source** - this can be:
- A companion agent like DrTrace (if installed)
- User-pasted log snippets
- Stack traces copied into chat
- Screenshots from monitoring dashboards
- Manual observations from the user

**The collaboration:**

| Source | Role | Data |
|--------|------|------|
| **Debugger** | Master of source code | DrSpec contracts, dependencies, invariants |
| **External Evidence** | Runtime truth | Logs, traces, patterns, frequencies |

**Why evidence matters:**

```
Problem: "Login fails with correct credentials"

Debugger alone:
  → "Check password validation logic, maybe encoding issue"
  → (Guessing based on code)

Debugger + Evidence:
  → Evidence: "100% of failures are users created before 2024-01-01"
  → Evidence: "Old users have $2b$ hashes, new users have $argon2id$"
  → Debugger: "Contract says verify_password uses argon2"
  → Combined: "Root cause is bcrypt→argon2 migration. Fix: support both."
  → (Evidence-based root cause)
```

**Analogy - The Flat Tire:**
- Without evidence: "Tire is flat. Replace it."
- With evidence: "80% of flat tires happen on Road A. Avoid Road A."

**How to use:**
1. Activate Debugger agent
2. Debugger queries contracts and dependencies
3. Provide evidence (paste logs, describe errors, or use DrTrace if available)
4. Debugger reasons from evidence to root cause, not just symptoms

**Note:** DrTrace is an example companion agent that can query logs programmatically. It's nice to have but not required - users can provide evidence manually.
