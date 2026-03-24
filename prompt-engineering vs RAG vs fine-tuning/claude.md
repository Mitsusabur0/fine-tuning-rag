User:
Context: We have a RAG agent for an AI concierge in a banking/finance webapp.

For each of [prompt-engineering, RAG, fine-tuning] explain: what they should be used for in our system; what they achieve; when to use them; if any are mutually exclusive or if we should use a combination of them.

Also, comment on where we should set the persona, style and branding of our AI concierge.

-----------------------------

Claude:
Here's a clear breakdown of each technique and how they work together in your banking AI concierge.

**Prompt engineering** is your always-on foundation. It's where you control the model's behavior in every single request — defining the persona, constraining scope, setting tone, and giving it rules like "never speculate on account balances" or "always recommend speaking with an advisor for investment decisions." It's lightweight to change (just edit the system prompt), which makes it ideal for anything that needs to evolve quickly: new product launches, seasonal messaging, compliance language updates. Use it to handle almost everything about *how* the model behaves.

**RAG** is what makes your concierge knowledgeable about *your* institution specifically. The base model knows nothing about your mortgage rates, fee schedules, branch hours, product eligibility rules, or internal policies — RAG retrieves that from a knowledge base and injects it into context at query time. This is what separates a generic financial chatbot from one that can actually answer "what's the current rate on your 5-year fixed mortgage?" or "do I qualify for the premium card?" Use RAG whenever the answer depends on proprietary, frequently-updated, or highly specific institutional data. It's also cheaper and more flexible than fine-tuning for factual recall.

**Fine-tuning** is the most expensive and least flexible of the three, so use it sparingly and deliberately. It's appropriate when you have a behavioral pattern that prompt engineering simply can't instill reliably at scale — for example, if you need the model to consistently produce responses in a very specific structured format for downstream processing, or if it needs to adopt a highly distinctive voice or reasoning style that doesn't hold up under a short system prompt. In banking, a common legitimate use case is training on domain-specific terminology, regulatory phrasing, or the subtle hedging language required for financial advice contexts. Fine-tuning bakes behaviors *into the weights*, which means they're hard to audit, slow to change, and expensive to redo — so it should complement prompt engineering and RAG, not replace them.

**None of these are mutually exclusive** — they stack. The typical production architecture for a system like yours is: fine-tune (optionally) for domain tone and format consistency → RAG for institutional knowledge retrieval → prompt engineering to tie everything together with persona, guardrails, and runtime rules. RAG + prompt engineering alone covers the vast majority of use cases well.

**On persona, style, and branding**: all of this belongs in the **system prompt** (prompt engineering), not in fine-tuning. Your AI concierge's name, voice, communication style, level of formality, and the bank's brand values should live in the system prompt as persistent instructions. This keeps them auditable, easy to update (rebrand, tone refresh, regulatory change), and testable without re-training anything. Fine-tuning for brand voice is a trap — it's slow, expensive, and you'll want to adjust it more often than you think.

Here's a visual of how the three layers interact at inference time:A few practical notes for your banking context specifically:

**Start without fine-tuning.** Most teams discover that a well-crafted system prompt plus good RAG coverage handles 90%+ of queries. Fine-tune only once you've identified specific, reproducible gaps that prompt engineering cannot close — don't assume you need it upfront.

**Keep your knowledge base fresh.** RAG is only as good as what's in it. Rate changes, new products, updated compliance language — your retrieval quality depends on a maintenance process for the knowledge base, not just the AI layer.

**Compliance and guardrails live in the system prompt.** Rules like "do not provide personalized investment advice," "always disclose that you are an AI," or "escalate to a human agent if the user expresses distress" belong in prompt engineering. They need to be explicit, auditable text that your compliance team can review — not buried in model weights.