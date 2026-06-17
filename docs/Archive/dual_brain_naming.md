# Naming the Two Brains — 100 Candidate Pairs

*Naming study for the Dual-Brain architecture of the AI-Native OS (Icebreaker).*
*Prepared June 17, 2026.*

---

## 1. The naming problem, stated precisely

The architecture has two language models with a deliberately asymmetric relationship. A good name **pair** should make that asymmetry legible at a glance.

| | **Brain 1 — currently "Quarantined Brain" (QB)** | **Brain 2 — currently "Privileged Brain" (PB)** |
|---|---|---|
| Layer | Perception | Execution |
| Faces | The user, documents, email, the web (the dirty outside) | Nothing external — only sanitized Intent Objects |
| Power | **Zero** execution; can't run a command, can't touch mcpd | **Root**; full MCP tool suite, real system access |
| Trust | Untrusted / exposed / contained | Trusted *because* it is blind |
| One-line essence | **Aware but powerless** | **Powerful but blind** |
| Model | Phi-4-mini (3.8B) | Qwen 2.5 Coder 1.5B (fine-tuned) |

The central tension every good pair encodes: **the one who sees cannot act; the one who acts cannot see.** That irony is the whole security model, so the strongest names lean into it. Throughout this document the order is always **QB name — PB name** (perceiver first, executor second).

---

## 2. How to read this document

- **100 pairs**, organized into **10 nomenclature families**, deliberately ranging from buttoned-up enterprise to mythic to playful.
- ⭐ marks a pair I think is genuinely strong.
- ⚠ marks a **known trademark / existing-name collision** found during the screen in §13 — usable, but check it.
- Your stated preference (mythic + enterprise) drives the **Top picks** in §3.
- §13 is the trademark-screen methodology and findings. **This is a preliminary screen, not legal clearance** — see the caveat there before you commit to one.

---

## 3. Top picks (mythic + enterprise blend)

If you want to stop reading and just choose, start here. These balance evocative weight with shippability and survived the collision screen.

| Rank | Pair (QB — PB) | Family | Why it wins |
|---|---|---|---|
| **1** | **Mimir — Magni** ⭐ | Mythic (Norse) | Mimir is the severed wise head that *counsels* Odin — pure perception, literally no body to act. Magni ("the mighty," Thor's son) is strength incarnate. Encodes *aware-but-powerless / powerful-but-blind* exactly, alliterates, and is almost collision-free. |
| **2** | **Augur — Artificer** ⭐ | Mythic / craft | The diviner who *reads signs* vs. the skilled maker who *builds*. Both "A," both dignified, both self-explaining once heard. Clean (minor: an Ethereum project "Augur" in a different domain). |
| **3** | **Sensor — Actuator** ⭐ | Functional | Control-systems canon. Instantly legible to any engineer; reads like real daemon names (`sensord` / `actuatord`). Maximum clarity, zero pretension. |
| **4** | **Interpreter — Executor** ⭐ | Enterprise | Says exactly what each brain does. Boring in the best way — perfect for docs, CLI, and onboarding. |
| **5** | **Argus — Atlas** ⭐ | Sentinels | Argus Panoptes, the hundred-eyed all-seeing watchman, vs. Atlas who *bears the weight*. Vivid, mythic, enterprise-safe. |
| **6** | **Echo — Golem** ⭐ | Mythic | Echo can only *repeat what she hears* (perception, no agency); the Golem *executes written instructions literally* — which is also a sly nod to the prompt-injection threat the architecture defends against. |
| **7** | **Ambassador — Magistrate** ⭐ | Statecraft | The envoy who deals with foreigners (the untrusted outside) vs. the official who holds binding authority at home. Corporate-credible and quietly elegant. |
| **8** | **Afferent — Efferent** ⭐ | Neuroanatomy | Afferent nerves carry signals *inward* (perception), efferent nerves carry them *outward to muscles* (action). Technically perfect; a great deep-cut for a systems crowd. |

**If I had to ship one today:** **Mimir / Magni** for a product with a story, or **Sensor / Actuator** if you want zero-explanation clarity. Both are clean in §13.

A nerd-favorite wildcard worth its own mention: **Read — Root** (perception is *read-only*; execution is *root*). It's in §12 and it's almost too on-the-nose in a good way.

---

## 4. Family A — Mythic & Mythological

The seer/messenger (perception) vs. the smith/maker/automaton (execution). This is where the *blind power* irony sings.

| # | QB — PB | Note |
|---|---|---|
| 1 | **Mimir — Magni** ⭐ | Counseling head vs. raw might. Top pick. Clean. |
| 2 | **Hermes — Hephaestus** | Messenger god vs. smith god. Strong, though Hermes is a famous fashion house (different class). |
| 3 | **Augur — Artificer** ⭐ | Reader of signs vs. maker. Clean-ish (⚠ minor: Augur, Ethereum). |
| 4 | **Echo — Golem** ⭐ | Repeats-only vs. literal executor. Injection metaphor baked in. |
| 5 | **Oracle — Vulcan** | Conceptually ideal (seer vs. forge) but ⚠ **Oracle is a massive tech trademark** — avoid as the headline name. |
| 6 | **Pythia — Daedalus** | Delphic oracle vs. master craftsman. ⚠ Pythia is an EleutherAI LLM — real ML-space collision. |
| 7 | **Muse — Forge** | Inspiration that speaks vs. the forge that makes. "Forge" is generic in dev tooling. |
| 8 | **Sibyl — Talos** | Prophetess vs. bronze automaton guardian. ⚠ avoid: "Sybil attack" is a security term with bad connotations. |
| 9 | **Iris — Hephaestus** | Rainbow-messenger goddess vs. smith. Iris is a common product name (minor). |
| 10 | **Delphi — Vulcan** | Oracle site vs. forge. ⚠ Delphi is an Embarcadero IDE. |

---

## 5. Family B — Sentinels & Guardians

The watcher who *spots* the threat but cannot strike, vs. the strong guardian who *acts* behind the wall. Security flavor, mythic edges.

| # | QB — PB | Note |
|---|---|---|
| 11 | **Argus — Atlas** ⭐ | Hundred-eyed watchman vs. world-bearer. Top-8 pick. |
| 12 | **Watcher — Warden** | Alliterative, vivid. ⚠ "Warden" is used (Minecraft mob; various tools). |
| 13 | **Vigil — Aegis** ⭐ | The watch vs. Zeus's shield. Clean and dignified. |
| 14 | **Picket — Rampart** | Forward sentry line vs. defensive wall. Underused, clean. |
| 15 | **Scout — Cerberus** | Forward eyes vs. the gate-guard that executes/blocks. |
| 16 | **Beacon — Citadel** | Signal vs. fortress. (Citadel: hedge fund + a game engine — minor.) |
| 17 | **Herald — Garrison** | Announcer vs. the armed force that acts. |
| 18 | **Heimdall — Tyr** | Watchman of the gods vs. war/justice god. ⚠ Heimdall is heavily used; Marvel owns the image. |
| 19 | **Lookout — Bastion** | ⚠ "Lookout" is a mobile-security company; "Bastion" is a generic infra term. |
| 20 | **Sentry — Marshal** | ⚠ avoid: "Sentry" is a well-known error-monitoring SaaS. |

---

## 6. Family C — Enterprise & Systems-Credible

Low-flair, shippable, self-documenting. These read like real services and survive a procurement review.

| # | QB — PB | Note |
|---|---|---|
| 21 | **Interpreter — Executor** ⭐ | Says what it does. Top-8 pick. |
| 22 | **Liaison — Operator** ⭐ | Front-of-house vs. the one who works the controls. |
| 23 | **Planner — Worker** ⭐ | Familiar agent-system split; reads instantly. |
| 24 | **Navigator — Pilot** ⭐ | Reads the charts vs. flies the plane. |
| 25 | **Advisor — Actuator** | Counsel vs. action. Alliterative. |
| 26 | **Intake — Runtime** | The intake desk vs. where things actually run. |
| 27 | **Resolver — Dispatcher** | Resolves intent vs. dispatches the work. |
| 28 | **Broker — Daemon** | Negotiates requests vs. the privileged background worker. |
| 29 | **Concierge — Engine** | Friendly front vs. the engine room. |
| 30 | **Frontline — Core** | Exposed edge vs. trusted center. |

---

## 7. Family D — Neuroanatomy & Dual-Brain

Since it is *literally* a dual-brain design, real brain structures are apt — and the perception/motor split exists in the anatomy already.

| # | QB — PB | Note |
|---|---|---|
| 31 | **Wernicke — Broca** ⭐ | Comprehension area vs. speech/motor-production area. Beautifully exact. |
| 32 | **Afferent — Efferent** ⭐ | Signals inward vs. outward-to-muscles. Top-8 pick. |
| 33 | **Sensory — Motor** | The two cortices. Plain and correct. |
| 34 | **Dendrite — Axon** | Receives vs. transmits/acts. Poetic. |
| 35 | **Limbic — Striatum** | Perception/affect vs. action-selection. |
| 36 | **Thalamus — Basal** | Sensory relay vs. basal-ganglia movement control. |
| 37 | **Occipital — Motor** | Visual perception vs. action. |
| 38 | **Percept — Effector** | Coined pair; clean and abstract. |
| 39 | **Cerebrum — Brainstem** | Higher cognition vs. autonomic execution. |
| 40 | **Cortex — Cerebellum** | Cognition vs. motor coordination. ⚠ "Cortex" is an ARM trademark + Palo Alto product. |

---

## 8. Family E — Perception ↔ Execution (plain functional)

Punchy, concrete, no lookup required. Great for casual reference even if the formal name is something grander.

| # | QB — PB | Note |
|---|---|---|
| 41 | **Sensor — Actuator** ⭐ | Control-systems canon. Top-3 pick. |
| 42 | **Eyes — Hands** ⭐ | The whole architecture in two words. |
| 43 | **Mind — Muscle** ⭐ | Alliterative, memorable. |
| 44 | **Lens — Lever** ⭐ | Perceives vs. applies force. Both "L." |
| 45 | **Listener — Doer** | Hears vs. acts. |
| 46 | **Voice — Hand** | Speaks vs. executes. |
| 47 | **Ear — Hand** | Minimal sensory/motor pair. |
| 48 | **Antenna — Engine** | Receives signal vs. drives action. |
| 49 | **Reader — Writer** | Perceives docs vs. writes to disk. (Slight clash with R/W jargon.) |
| 50 | **Intake — Output** | Plainest possible framing. |

---

## 9. Family F — Security, Sandbox & Containment

Leans directly on the project's own vocabulary (quarantine, sandbox, vault). The exposed chamber vs. the hardened core.

| # | QB — PB | Note |
|---|---|---|
| 51 | **Airlock — Bastion** ⭐ | Exposed entry chamber vs. secure stronghold. Vivid. |
| 52 | **Moat — Keep** ⭐ | Outer exposed water vs. the inner fortified tower. |
| 53 | **Perimeter — Enclave** ⭐ | Exposed edge vs. trusted execution enclave (TEE echo). |
| 54 | **Membrane — Nucleus** | Cell wall faces outside vs. protected core. |
| 55 | **Sandbox — Strongbox** | Contained play area vs. the locked box. |
| 56 | **Decon — Cleanroom** | Decontamination vs. the sterile interior. |
| 57 | **Buffer — Enclave** | Absorbs the outside vs. the protected zone. |
| 58 | **Foyer — Vault-Room** | Public entry vs. the inner vault. (Avoid bare "Vault," see ⚠ below.) |
| 59 | **Quarantine — Vault** | Matches the doc terms exactly, but ⚠ **Vault is a HashiCorp trademark**. |
| 60 | **Greenroom — Core** | Where the untrusted waits vs. the trusted center. |

---

## 10. Family G — Statecraft & Diplomacy

The envoy who deals with foreigners (untrusted outside) vs. the sovereign/official who holds binding authority but stays in the palace.

| # | QB — PB | Note |
|---|---|---|
| 61 | **Ambassador — Magistrate** ⭐ | Top-8 pick. Dignified, corporate-safe. |
| 62 | **Emissary — Steward** ⭐ | Sent out to negotiate vs. the one who manages the estate. |
| 63 | **Diplomat — Marshal** | Negotiates vs. enforces. |
| 64 | **Herald — Chancellor** | Announces vs. governs. |
| 65 | **Attaché — Regent** | Junior envoy vs. the ruling authority. |
| 66 | **Interpreter — Sovereign** | Court translator vs. the one who decides. |
| 67 | **Negotiator — Executive** | Talks terms vs. acts on them. |
| 68 | **Envoy — Sovereign** | Conceptually clean but ⚠ **Envoy is a major CNCF proxy**. |
| 69 | **Legate — Praetor** | Roman envoy vs. magistrate/commander. ⚠ "Praetorian" is a cybersecurity firm. |
| 70 | **Consul — Tribune** | ⚠ avoid: "Consul" is a HashiCorp product. |

---

## 11. Family H — Oracles, Scribes & Archetypal Roles

Craft/role archetypes with mythic warmth but no proper-noun baggage — most of these are clean.

| # | QB — PB | Note |
|---|---|---|
| 71 | **Seer — Smith** ⭐ | The cleanest archetype pair: foresees vs. forges. No collision. |
| 72 | **Witness — Executor** ⭐ | Legal framing: one perceives, one carries out. |
| 73 | **Scribe — Mason** | Records/reads vs. builds. |
| 74 | **Herald — Golem** | Announces vs. literal-minded executor. |
| 75 | **Bard — Wright** | Tells vs. makes (ship-/wheel-wright). |
| 76 | **Scout — Sapper** | Reads terrain vs. does the engineering/demolition. |
| 77 | **Counsel — Agent** | Advises vs. acts on instructions. |
| 78 | **Curator — Operator** | Selects/presents vs. runs the machinery. |
| 79 | **Reader — Wright** | Perceives vs. constructs. |
| 80 | **Augur — Mason** | Diviner vs. builder. |

---

## 12. Family I — Celestial & Elemental

Twin-body and light/fire symbolism. The visible sky-phenomenon (perception) vs. the deep/hidden power (execution).

| # | QB — PB | Note |
|---|---|---|
| 81 | **Castor — Pollux** ⭐ | The mythic twins; the mortal/immortal duality maps to exposed/protected. (⚠ minor: a Eurorack module + a bridge-analysis tool, both unrelated domains.) |
| 82 | **Halo — Hearth** ⭐ | Outer ring of light vs. the inner fire that does the work. |
| 83 | **Helios — Vulcan** | The all-seeing sun vs. the forge in the dark. |
| 84 | **Luna — Sol** | Reflective night-eye vs. the powering sun. |
| 85 | **Aurora — Magma** | Visible sky-glow vs. deep molten power. |
| 86 | **Corona — Core** | Outer atmosphere vs. the stellar core. |
| 87 | **Zenith — Nadir** | The visible high point vs. the hidden depth. |
| 88 | **Polaris — Forge** | The star you navigate by vs. where work is made. |
| 89 | **Eclipse — Ember** | Shadowed perception vs. banked execution heat. |
| 90 | **Lumen — Anvil** | Unit of perceived light vs. the surface action lands on. |

---

## 13. Family J — Minimalist, Codenames & Wordplay

Short, sharp, and a little fun. Good for internal codenames, env vars, and CLI even if the public name is grander.

| # | QB — PB | Note |
|---|---|---|
| 91 | **Q — P** ⭐ | Literally Quarantined / Privileged. Frictionless internal codename. |
| 92 | **Read — Root** ⭐ | Perception is *read-only*; execution is *root*. Near-perfect wordplay. |
| 93 | **Say — Sudo** ⭐ | One speaks; the other executes with privilege. Nerdy and apt. |
| 94 | **Yin — Yang** | Complementary duality. (Generic but universally understood.) |
| 95 | **Echo — Edict** | Repeats vs. issues binding commands. |
| 96 | **Fore — Aft** | The exposed bow vs. the engine-room stern. |
| 97 | **Talk — Do** | The plainest possible split. |
| 98 | **In — Out** | Intake vs. output. |
| 99 | **Mouth — Fist** | Speaks vs. strikes. |
| 100 | **Front — Core** | Exposed face vs. trusted center. |

---

## 14. Trademark & existing-name screen

**Method.** I web-searched the most prominent and highest-risk candidates against existing tech products, open-source projects, and trademarks, and checked security-domain candidates for unwanted connotations. I did **not** exhaustively clear all 100 names, and this is **not** a legal opinion. Before adopting a public name, do a proper USPTO/EUIPO search and a GitHub/package-registry check for the specific word *and* the pair.

**Confirmed collisions / cautions (avoid as the headline brand):**

| Name | Conflict found |
|---|---|
| **Oracle** | Oracle Corporation — one of the largest tech trademarks; also a database/OS vendor. Hard avoid. |
| **Vault, Consul, Nomad** | HashiCorp registered products. Avoid. |
| **Envoy** | CNCF service-proxy project — directly in the infra/systems space. Avoid for a system component. |
| **Cortex** | Registered ARM trademark; also Palo Alto "Cortex XDR." Avoid. |
| **Sentinel / Sentry** | Thales "Sentinel," Microsoft Sentinel, SentinelOne; "Sentry" = the error-monitoring SaaS. Avoid. |
| **Athena** | AWS Athena + SentinelOne "Athena." Avoid. |
| **Pythia** | EleutherAI's Pythia LLM — same domain (language models). Avoid. |
| **Janus** | Meetecho Janus WebRTC server + Microsoft "Project Janus." Conceptually ideal (two-faced) but taken. |
| **Heimdall** | Many security/gateway tools; Marvel owns the name/image. Caution. |
| **Sibyl** | Connotation clash: "Sybil attack" is a known security failure mode. Avoid for a security product. |

**Minor / different-domain (usable, verify for your jurisdiction & class):** Augur (Ethereum), Delphi (IDE), Citadel (finance/game engine), Lookout (mobile security), Castor & Pollux (Eurorack + bridge tool), Iris, Praetorian (security firm — affects "Praetor").

**Clean as far as the screen went (still verify before shipping):** Mimir/Magni, Augur/Artificer, Sensor/Actuator, Interpreter/Executor, Argus/Atlas, Echo/Golem, Ambassador/Magistrate, Afferent/Efferent, Wernicke/Broca, Seer/Smith, Airlock/Bastion, Moat/Keep, Read/Root, Q/P.

---

## 15. Recommendation

For a name with a **story** that still survives a boardroom: **Mimir — Magni**. For **instant legibility** with zero explanation: **Sensor — Actuator** (or **Interpreter — Executor**). For something **distinctively mythic-yet-credible**: **Argus — Atlas** or **Augur — Artificer**.

Whatever you pick, run the formal trademark + registry check in §14 on the final pair, and keep `Q` / `P` as the internal shorthand regardless — it maps cleanly to the existing Quarantined/Privileged vocabulary and the code.
