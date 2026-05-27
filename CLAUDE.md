# Yaldabaoth -- Desktop Control MCP

## Daemon
Run `python desktop_control.py --allow-input` first. Verify: `vk_status()`.

## Tools
- `vk_hotkey(keys)` -- key chords ("win r", "ctrl l")
- `vk_press(key)` -- single key ("enter", "tab")
- `vk_type(text)` -- type text
- `vk_sweep(stop_if?, compact=True)` -- enumerate all focusable elements
- `vk_observe_window()` / `vk_observe_focus()` -- state queries
- `vk_snapshot()` -- screenshot + UIA state (for your vision)
- `vk_route(steps)` -- multi-step sequences (hotkey, press, type, sleep, tab_sweep, etc.)
- `vk_status()` -- bridge health
- `vk_click(target)` -- OCR the screen and click matching text
- `vk_ocr()` -- OCR the screen, return all visible text with positions
- `vk_mouse_move(x, y)` -- move cursor to screen coordinates
- `vk_vision(goal)` -- screenshot to Gemini browser, returns structured elements
- `vk_gemini_click(goal)` -- vision-guided click via Gemini
- `vk_recall(task)` -- look up the best known strategy before acting
- `vk_learn(task, approach_id, steps, success)` -- record an attempt result
- `vk_experiment(task)` -- run one iteration of the research ratchet loop

## Navigation pattern
1. `vk_sweep(compact=True)` -- get all elements with name + role
2. **You decide** which element matches the goal (read the list)
3. `vk_press("tab")` N times to reach it (or use `vk_sweep(stop_if={"name_or_status_contains_any": ["target"]}, activate_on_match=True)`)
4. `vk_press("enter")` to activate

## Rules
- **You are the reasoner** -- the daemon is a dumb I/O layer. Read sweep output, decide, act.
- **Always use compact mode** -- `vk_sweep(compact=True)` returns ~2KB vs ~90KB full.
- For multi-step setup, use `vk_route` with slim steps: `[{"hotkey": "win r"}, {"sleep": 0.5}, {"type": "notepad"}, {"press": "enter"}]`

## AutoResearch Loop
- Run `python research_loop.py` to start the autonomous research loop
- The loop reads `program.md` for instructions and `strategies.json` for known approaches
- Every experiment is logged to `experiment_log.jsonl`
- Successful experiments are git-committed; failures are logged but not committed

## Strategy Memory
- `vk_recall(task)` -- look up the best known approach before acting
- `vk_learn(task, approach_id, steps, success)` -- record an attempt result
- `vk_experiment(task)` -- run a single research iteration
- The system learns from every attempt. Check `strategies.json` for the current knowledge base.

## Karpathy Coding Guidelines

### 1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.
- State assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them -- don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.
- **Desktop control**: always `vk_recall(task)` before trying something new.

### 2. Simplicity First
Minimum code that solves the problem. Nothing speculative.
- No features beyond what was asked.
- No abstractions for single-use code.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.
- **Desktop control**: prefer keyboard over mouse, short routes over long ones.

### 3. Surgical Changes
Touch only what you must. Clean up only your own mess.
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken. Match existing style.
- Remove imports/variables/functions that YOUR changes made unused.
- Every changed line should trace directly to the user's request.
- **Desktop control**: change one thing per experiment, record what changed.

### 4. Goal-Driven Execution
Define success criteria. Loop until verified.
- Transform tasks into verifiable goals with concrete checks.
- For multi-step tasks, state a brief plan with verification at each step.
- Strong success criteria let you loop independently. Weak criteria require constant clarification.
- **Desktop control**: always verify the outcome with `vk_observe_window()` or `vk_ocr()`.
