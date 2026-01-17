# Markdown rules

This file list the markdown rules used in DRSpec documentation. The agent MUST read this file and follow these rules when reading markdown content.

## Scope

Each section in the documentation has specific keywords:
- [M]andatory: This section MUST read and follow content in the section.
- [O]ptional: This section MAY read and follow content in the section.
- [M]enu: This section has description that has content that need to show to users for confirmation or information purposes.

If a section is not marked, it is considered optional. A section can have multiple keywords, for example [M][O] means the section is mandatory but some content in the section is optional. When a section is marked (for example [M]), all sub-sections inherit the same marking unless they have their own marking. For example:

```
## Section 1 [M]
### Sub-section 1.1     -> Mandatory because parent is [M]
### Sub-section 1.2 [O]
## Section 2
### Sub-section 2.1 [M]
### Sub-section 2.2     -> Optional because parent is not marked
```

## Rules

- Always remind or reread if necessary before executing any next step or task including these rules. Always remind yourself if my next action is violating any rule. (IMPORTANT)
- Follow the instructions/rules is the highest priority, don't do any shortcut or you will be fired.
- Just simply follow the rules, descritions, principles, and menu when reading markdown content, don't over‑interpret the content. Don't think you can guess what the user wants or what the content is about, just follow the rules.
