# Yaldabaoth AutoResearch Program

## Current Goal
Learn reliable strategies for common Windows desktop tasks.

## Active Research Tasks
- Open Chrome browser
- Open Notepad
- Open File Explorer
- Navigate browser to a URL
- Switch between open windows
- Open a new browser tab
- Close the current browser tab
- Search for text on a page
- Copy and paste text

## Verification Methods
- Title check: window title contains expected text after action
- OCR check: screen contains expected text after action
- Element check: UIA element with expected name/role exists after action

## Rules
- ONE approach variation per experiment
- Record every attempt, whether it succeeds or fails
- When an approach reaches >95% success rate over 20+ attempts, mark it as proven
- When an approach fails >80% of the time over 10+ attempts, prune it
- Never modify research_loop.py or strategy_engine.py during experiments
- Wait for the previous action to complete before verifying
- Always check window focus before sending keystrokes
