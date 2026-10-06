# Demo video script (2:30 to 3:00)

Pitch it like you're talking to someone who has never heard of the project and has 3 minutes. Record the screen with OBS or Xbox Game Bar (Win+G), and the phone with iPhone screen recording.

**Before recording**

* `streamlit run app.py`, browser at 100% zoom, window maximized, sidebar open.
* Run the agent once off-camera so the menus are cached (faster on camera).
* Set your real prefs in the sidebar (diet, protein goal).
* Have the phone notification ready: run `python -m src.morning_run --force` once so it's on your lock screen, then screen-record the phone.

| Time | Screen | Voiceover |
|---|---|---|
| 0:00 to 0:15 | Nutrislice on your phone or browser: scroll East, switch to West, tap into an item to see protein | "Every day, thousands of Stony Brook students do this: open the menu, scroll East, scroll West, tap into items one by one to check protein or allergens. Then do it again for lunch and dinner." |
| 0:15 to 0:30 | Same, fast-forwarded | "Two halls, 30-plus stations, about 300 items. So most of us just eat whatever's closest." |
| 0:30 to 0:45 | DineWolfie home screen | "This is DineWolfie, an AI agent that plans your whole day of eating at East and West, from today's real menus." |
| 0:45 to 1:35 | Type: "I'm vegetarian, aiming for 120 g protein, and I'll be near West at lunch." Press **Plan my day**. Point at tickets as they appear. | "I tell it my goal. First it reads its memory of me, my diet, my goal, and what I ate recently. It writes a plan. Then it acts: it pulls West's lunch menu, compares both halls for dinner, searches for high-protein vegetarian items, and adds up the nutrition in code, so it can't make numbers up. Finally it submits the plan to a checker that confirms every item is really on that hall's menu and safe for me." |
| 1:35 to 1:45 | Zoom on the tray: the swap box under lunch, the treat tag, then open **Teach DineWolfie** and tap **Never again** on one item | "It's not a calculator, it's a friend who knows the dining halls. Every meal comes with a swap in case the line is long. It slips in a treat when my goal allows. And if I never want to see an item again, one tap and it's blocked for good." |
| 1:45 to 2:05 | Sidebar: turn on **West dinner not posted**. Run again. Zoom on the amber **Changed course** ticket. | "Things go wrong in real life. Here West hasn't posted dinner. The agent notices, switches dinner to East, and tells me exactly what it changed and why. And when my protein goal isn't reachable, it tells me how many grams short I am and how to fix it." |
| 2:05 to 2:20 | Phone lock screen with the 8 AM notification | "And I don't even have to ask. Every morning at 8, it runs by itself and sends the plan to my phone." |
| 2:20 to 2:40 | **How it works** tab (architecture diagram) | "Under the hood: Claude, through the Claude Agent SDK, runs the loop. It plans, uses 11 tools on real SBU menu data and my memory, observes every result, adapts, and finishes with a verified plan." |
| 2:40 to 3:00 | Back to the tray | "Next: more campus locations, calendar-aware routing to the hall nearest your class, and group plans. DineWolfie: stop scrolling, start eating." |

**Tips**

* Speak slowly, and cut dead time in editing. The agent takes a while, so speed up the waiting parts 2x but keep the adapt ticket at normal speed.
* Make the adapt moment obvious: pause on it for 2 to 3 seconds.
* Upload to YouTube as **Unlisted** and paste the link into Devpost.
