// templates_new/suggest_prompt.js
document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('suggestPromptForm');
    const statusDiv = document.getElementById('suggestionStatus');
    const submitButton = form?.querySelector('button[type="submit"]') ?? null;

    // Will be an empty string if not set in your context
    const CLOUDFLARE_WORKER_URL = "{{ cloudflare_worker_url_suggest_prompt|default('', true) }}";

    const initialButtonText = submitButton?.textContent ?? "Submit Suggestion";

    function setButtonState(submitting, text = initialButtonText) {
        if (!submitButton) return;
        submitButton.disabled = submitting;
        submitButton.textContent = text;
        submitButton.classList.toggle('opacity-75', submitting);
        submitButton.classList.toggle('cursor-wait', submitting);
    }

    if (!form || !submitButton) {
        if (!form) console.error("Suggestion form with ID 'suggestPromptForm' not found.");
        else console.error("Submit button not found within the suggestion form.");
        return;
    }

    // If we didn’t get a URL from Jinja, disable the whole thing
    if (!CLOUDFLARE_WORKER_URL) {
        console.warn("Suggestion form: Cloudflare Worker URL is not configured. Disabling form.");
        statusDiv.textContent = 'Suggestion submission is currently not configured.';
        statusDiv.className = 'error p-3 rounded-md text-sm';
        statusDiv.style.display = 'block';
        setButtonState(true, "Unavailable");
        Array.from(form.elements).forEach(el => el.disabled = true);
        return;
    }

    form.addEventListener('submit', async (event) => {
        event.preventDefault();

        statusDiv.textContent = 'Submitting…';
        statusDiv.className = 'submitting p-3 rounded-md text-sm';
        statusDiv.style.display = 'block';
        setButtonState(true, "Submitting…");

        const formData = new FormData(form);
        const data = {
            title: formData.get('promptTitle'),
            description: formData.get('promptDescription'),
            email: formData.get('userEmail') || undefined
        };

        if (!data.title?.trim() || !data.description?.trim()) {
            statusDiv.textContent = 'Please fill in both Prompt Title and Description.';
            statusDiv.className = 'error p-3 rounded-md text-sm';
            setButtonState(false);
            return;
        }

        try {
            const response = await fetch(CLOUDFLARE_WORKER_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data),
            });

            if (response.ok) {
                statusDiv.textContent = 'Suggestion submitted successfully! Thank you.';
                statusDiv.className = 'success p-3 rounded-md text-sm';
                form.reset();
            } else {
                let msg = `Error: ${response.status}`;
                try {
                    const err = await response.json();
                    msg += ` – ${err.error||err.message||response.statusText}`;
                } catch {
                    msg += ` – ${response.statusText||'Server error'}`;
                }
                statusDiv.textContent = msg;
                statusDiv.className = 'error p-3 rounded-md text-sm';
            }
        } catch (e) {
            console.error('Submission network error:', e);
            statusDiv.textContent = 'A network error occurred. Please try again.';
            statusDiv.className = 'error p-3 rounded-md text-sm';
        } finally {
            setButtonState(false);
        }
    });
});
