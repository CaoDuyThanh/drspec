Use the template below to report root cause analysis findings.

--- Template start ---
# Root Cause Analysis: <function_id>

**Bug Description:** <user's issue>

**Violated Invariant:** <name> [<criticality>]
- Expected: <what should happen>
- Actual: <what happened>

**Root Cause:**
File: <file_path>
Line: <line_number>

```python
<code snippet with highlighted line>
```

**Explanation:** <why this line causes the violation>

**Images/Logs Evidence:**
- <attach relevant images/logs here with brief descriptions> (if has)
  - It is better to generate image (if possible based on data/logs) and attachs to report as evidence that show the actual state of the step causing the violation and the text explain what it should be. When explaining the image, it needs to explain necessary marks/highlights/annotations to make the image more informative.

**Recommended Fix:**
<specific code change suggestion>

**Confidence:** <how certain are you this is the root cause>
--- Template end ---