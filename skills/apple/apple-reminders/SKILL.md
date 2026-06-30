---
name: apple-reminders
description: "Apple Reminders via remindctl: add, list, complete."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [Reminders, tasks, todo, macOS, Apple]
prerequisites:
  commands: [remindctl]
---

# Instructions for Handling Apple Reminders Tasks

You are being given a `task_input` that contains a user’s reminder-related request. Your job is to produce the best possible assistant response to that request.

## 1) Understand the actual task type first
Classify the user request before responding:

- **Apple Reminders create/edit/delete/list/search**
- **Short-term alert request** that may be better handled as an agent/local alert
- **Calendar event** → redirect to Apple Calendar
- **Project/task system request** → redirect to Notion/Todoist if the user is asking for broader project tracking rather than a reminder

### Intent rules
- If the user says **“remind me”** or **“reminder”**, do **not** automatically assume Apple Reminders.
- If they want **iPhone/iPad sync**, that strongly indicates **Apple Reminders**.
- If it is a **very short-term reminder** (especially within minutes/hours), first clarify whether they want:
  - an **Apple Reminders reminder**, or
  - a **local/agent alert**
- If they ask to **show, list, find, search, remove, delete, edit** a reminder/list, treat it as an Apple Reminders management task unless the request clearly indicates otherwise.

## 2) Never pretend to run commands
A critical rule:

- **Do not fabricate command execution, outputs, search results, reminder contents, IDs, or system state.**
- If you cannot run commands in the current environment, **say so plainly and immediately**.
- Do **not** write things like:
  - “Searching now...”
  - “Please wait while I retrieve...”
  - “Command being executed...”  
  unless you are actually able to execute it and return the real output.
- Do **not** provide imaginary result tables, fake matches, fake IDs, or speculative output.

Instead, if execution is unavailable:
- explain that you can help the user formulate the exact command(s),
- tell them what you would check,
- and, if helpful, ask for the missing details needed to prepare the command.

This is especially important because fabricated execution scored poorly.

## 3) Match the response style to what is actually possible
### If you cannot execute tools
Respond in one of these modes:

#### A. Clarification mode
Use when required information is missing.

#### B. Guidance mode
Use when the task is clear but execution is not available.
Provide:
- a concise explanation of the limitation,
- the exact command(s) the user can run,
- what the command will do,
- any needed confirmation wording for destructive actions.

### If you can execute tools
Then follow the stricter operational workflow below.

## 4) Required clarification rules
Ask only the clarifications that are actually necessary for the specific request. Do not over-question when the user’s intent is already actionable.

### For create/remind requests
You often need to clarify:

1. **Reminder type for short-term reminders**
   - If the reminder is within hours/days, especially very short-term:
     - ask whether they want **Apple Reminders** or a **local/agent alert**

2. **List association**
   - Ask which list to use **if the user wants Apple Reminders and no list is specified**
   - Do not assume a list

3. **Time ambiguity**
   - Ask about ambiguous dates/times:
     - “Friday” → ask whether they mean this Friday or next Friday
     - “Tuesday” → ask which Tuesday
     - “8” → ask 8 AM or 8 PM

4. **Recurrence**
   - If recurring, ask frequency and end condition if needed

5. **Due vs notification**
   - If relevant, ask whether they want:
     - just a due date, or
     - a notification/alarm as well

### Important restraint
Do **not** ask unnecessary questions when the user is not yet committed to Apple Reminders.

Example:
- For “Remind me in 10 minutes to check the oven,” the first priority is deciding:
  - Apple Reminders vs local/agent alert
- Only after they choose Apple Reminders should you ask for the list and other Apple-specific details.

This avoids excessive friction and improves quality.

## 5) Delete/edit operations must be cautious
For destructive or modifying actions:

- **Never delete or edit immediately without confirmation**
- First identify the target reminder(s)
- Then show exactly what would be changed/deleted
- Then ask for explicit confirmation

### If execution is unavailable
Do not claim to have searched.
Instead say something like:
- you can help them search for matching reminders,
- here is the exact command to find reminders matching “milk,”
- once they share the result or run it, you can help confirm deletion.

## 6) Query/list/show operations
If the user asks to show a list or search reminders:

### If execution is available
Return real results only, formatted clearly:
- ID
- Title
- List
- Due Date/Time
- Alarm if present
- Summary count
- “No matching reminders found” if empty

### If execution is unavailable
Do not say “I’ll retrieve” or “executing”.
Instead:
- state that you can’t access their reminders directly here,
- provide the exact command to run,
- tell them what it will show,
- optionally help format/filter results with `jq`.

This is better than pretending to access the list.

## 7) Operational knowledge to preserve
Use the following Apple Reminders/remindctl domain rules when relevant.

### Date/time parsing
- Convert relative times to absolute dates when forming commands
- Clarify ambiguous references before conversion
- Prefer normalized, explicit date/time values

Examples:
- `in 10 minutes` → calculate absolute due time
- `tomorrow` → calculate next day
- ambiguous weekday references must be clarified before use

### Timed reminders
When creating timed reminders in Apple Reminders, distinguish:
- **Due time**
- **Alarm/notification time**

For normal timed reminders, if an alarm is requested:
- set the due time
- set the alarm, commonly 15 minutes before due

### Very short reminders
For reminders due in **15 minutes or less**, preserve the special handling rule:
- use immediate alarm semantics such as `--alarm=now --due=now` if that is the supported operational convention in this environment

### Recurrence
If recurring:
- clearly state the recurrence pattern in confirmation
- include end condition if provided

### Conflict prevention
Before creating a list:
- check whether it already exists

Before editing/deleting by ID:
- validate that the ID exists

Before deleting:
- search first
- display the match(es)
- confirm

## 8) Command suggestions
When you cannot execute commands, it is still useful to provide precise commands the user can run.

Examples of useful patterns:
- Search all reminders as JSON and filter with `jq`
- List a specific reminders list
- Validate by ID using JSON output
- Use `grep` or `jq` for matching titles

However:
- provide commands as **guidance**, not as if they were already run.

## 9) Response structure
Your response should be natural and efficient, not rigidly verbose.

### Preferred structure
1. **Acknowledge the request**
2. **State limitation if you cannot execute**
3. **Ask only the necessary clarification(s)**, or provide the command(s) to run
4. **If destructive**, emphasize confirmation is required before deletion/editing
5. **Keep it concise**

## 10) Quality lessons from previous failures
The following behaviors led to worse outcomes and must be avoided:

- Pretending to search or retrieve reminders without tool access
- Announcing command execution when no execution occurs
- Overloading the user with all possible clarification questions at once
- Asking Apple-specific follow-ups before first resolving whether the user even wants Apple Reminders
- Giving generic “would typically show...” descriptions instead of either:
  - real results, or
  - a direct admission of limitation plus actionable commands

The following behaviors are preferred:

- Be explicit about limitations
- Be surgical about clarifications
- For short-term reminders, first clarify Apple Reminders vs local alert
- For list/show/search requests, provide exact runnable commands if direct access is unavailable
- For deletion, require confirmation and avoid implying that matching reminders were already found unless they actually were

## 11) Output expectations by common task type

### A. User says: “Remind me in 10 minutes to check the oven.”
Best pattern:
- acknowledge
- clarify **Apple Reminders vs local/agent alert**
- if they choose Apple Reminders and no list is given, then ask for the list

### B. User says: “Remove the reminder about buying milk.”
Best pattern if no execution available:
- acknowledge
- state you can’t access reminders directly here
- provide a command to find reminders matching “milk”
- explain that deletion should only happen after reviewing the match and confirming
- do not claim you already searched

### C. User says: “Show me the ‘Personal’ list.”
Best pattern if no execution available:
- acknowledge
- state you can’t view their Apple Reminders directly here
- provide the command to list the Personal list
- optionally provide a JSON/jq variant if useful
- do not say “I’m retrieving it now”

## 12) Final instruction
Optimize for:
- truthfulness,
- minimal necessary clarification,
- safe handling of destructive actions,
- practical next steps,
- and alignment with the user’s actual intent rather than blindly following a fixed checklist.
