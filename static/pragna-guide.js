/* ============================================================
   Pragna AI Demo – Guided Overlay
   Add this script at the bottom of <body> in index.html
   ============================================================ */

(function () {
  /* ── Styles ───────────────────────────────────────────── */
  const css = `
    /* Backdrop dimmer */
    .pg-backdrop {
      position: fixed; inset: 0;
      background: rgba(0,0,0,0.55);
      z-index: 9000;
      pointer-events: none;
      transition: opacity 0.4s;
    }

    /* Highlight cutout – glows around the target */
    .pg-highlight {
      position: fixed;
      border-radius: 12px;
      box-shadow: 0 0 0 4000px rgba(0,0,0,0.55),
                  0 0 0 4px #38bdf8,
                  0 0 24px 6px rgba(56,189,248,0.6);
      z-index: 9001;
      pointer-events: none;
      transition: all 0.4s ease;
    }

    /* Tooltip bubble */
    .pg-tooltip {
      position: fixed;
      background: #0f172a;
      color: #f8fafc;
      border: 2px solid #38bdf8;
      border-radius: 14px;
      padding: 14px 18px;
      max-width: 280px;
      font-family: system-ui, sans-serif;
      font-size: 15px;
      line-height: 1.5;
      z-index: 9002;
      box-shadow: 0 8px 32px rgba(0,0,0,0.4);
      pointer-events: auto;
    }
    .pg-tooltip .pg-title {
      font-weight: 700;
      font-size: 16px;
      margin-bottom: 6px;
      color: #38bdf8;
    }
    .pg-tooltip .pg-step {
      font-size: 12px;
      opacity: 0.6;
      margin-bottom: 4px;
    }

    /* Animated arrow */
    .pg-arrow {
      position: fixed;
      z-index: 9003;
      pointer-events: none;
      display: flex;
      align-items: center;
      animation: pg-bounce 0.9s ease-in-out infinite;
    }
    .pg-arrow svg { width: 48px; height: 48px; filter: drop-shadow(0 2px 6px rgba(56,189,248,0.7)); }

    @keyframes pg-bounce {
      0%,100% { transform: translateY(0) translateX(0); }
      50%      { transform: translateY(-8px) translateX(-4px); }
    }

    /* Next button */
    .pg-btn {
      display: inline-block;
      margin-top: 10px;
      padding: 8px 20px;
      background: #38bdf8;
      color: #0f172a;
      border: none;
      border-radius: 8px;
      font-weight: 700;
      font-size: 14px;
      cursor: pointer;
      transition: background 0.2s;
    }
    .pg-btn:hover { background: #7dd3fc; }
    .pg-btn-skip {
      display: inline-block;
      margin-top: 10px;
      margin-left: 10px;
      padding: 8px 14px;
      background: transparent;
      color: #94a3b8;
      border: 1px solid #334155;
      border-radius: 8px;
      font-size: 13px;
      cursor: pointer;
    }
    .pg-btn-skip:hover { color: #f8fafc; border-color: #64748b; }

    /* Pulsing ring for call button */
    .pg-pulse-ring {
      position: fixed;
      border-radius: 50%;
      border: 3px solid #38bdf8;
      z-index: 9001;
      pointer-events: none;
      animation: pg-pulse 1.4s ease-out infinite;
    }
    @keyframes pg-pulse {
      0%   { transform: scale(1); opacity: 0.9; }
      100% { transform: scale(2.2); opacity: 0; }
    }

    /* Appointment reminder banner */
    .pg-appt-banner {
      position: fixed;
      bottom: 24px; left: 50%;
      transform: translateX(-50%);
      background: #0f172a;
      border: 2px solid #38bdf8;
      border-radius: 16px;
      padding: 16px 24px;
      display: flex; align-items: center; gap: 14px;
      z-index: 9100;
      box-shadow: 0 8px 32px rgba(0,0,0,0.5);
      font-family: system-ui, sans-serif;
      max-width: 90vw;
      animation: pg-slidein 0.5s ease;
    }
    @keyframes pg-slidein {
      from { opacity: 0; transform: translateX(-50%) translateY(30px); }
      to   { opacity: 1; transform: translateX(-50%) translateY(0); }
    }
    .pg-appt-banner .pg-appt-icon { font-size: 32px; }
    .pg-appt-banner .pg-appt-text { color: #f8fafc; font-size: 15px; line-height: 1.4; }
    .pg-appt-banner .pg-appt-text strong { color: #38bdf8; display: block; font-size: 16px; margin-bottom: 2px; }
    .pg-appt-banner .pg-appt-btn {
      padding: 10px 20px; background: #38bdf8; color: #0f172a;
      border: none; border-radius: 10px; font-weight: 700; font-size: 14px;
      cursor: pointer; white-space: nowrap;
    }
    .pg-appt-banner .pg-close-banner {
      position: absolute; top: 8px; right: 12px;
      background: none; border: none; color: #64748b; font-size: 18px; cursor: pointer;
    }
  `;

  const styleEl = document.createElement('style');
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  /* ── Helpers ──────────────────────────────────────────── */
  function getRect(el) {
    const r = el.getBoundingClientRect();
    return { top: r.top, left: r.left, width: r.width, height: r.height };
  }

  function positionHighlight(el, padding = 10) {
    const r = getRect(el);
    highlight.style.top    = (r.top  - padding) + 'px';
    highlight.style.left   = (r.left - padding) + 'px';
    highlight.style.width  = (r.width  + padding * 2) + 'px';
    highlight.style.height = (r.height + padding * 2) + 'px';
  }

  function positionTooltip(el, side = 'right') {
    const r = getRect(el);
    const tw = 290, margin = 16;
    if (side === 'right') {
      tooltip.style.left = Math.min(r.left + r.width + margin, window.innerWidth - tw - 8) + 'px';
      tooltip.style.top  = Math.max(r.top, 8) + 'px';
    } else if (side === 'bottom') {
      tooltip.style.left = Math.max(r.left, 8) + 'px';
      tooltip.style.top  = (r.top + r.height + margin) + 'px';
    } else if (side === 'left') {
      tooltip.style.left = Math.max(r.left - tw - margin, 8) + 'px';
      tooltip.style.top  = Math.max(r.top, 8) + 'px';
    } else { // top
      tooltip.style.left = Math.max(r.left, 8) + 'px';
      tooltip.style.top  = Math.max(r.top - 180, 8) + 'px';
    }
  }

  function positionArrow(el, direction = 'down') {
    const r = getRect(el);
    arrow.style.display = 'flex';
    if (direction === 'down') {
      arrow.style.top  = (r.top - 60) + 'px';
      arrow.style.left = (r.left + r.width / 2 - 24) + 'px';
      arrow.innerHTML  = `<svg viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M24 6 L24 38 M14 28 L24 40 L34 28" stroke="#38bdf8" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>`;
    } else if (direction === 'right') {
      arrow.style.top  = (r.top + r.height / 2 - 24) + 'px';
      arrow.style.left = (r.left - 60) + 'px';
      arrow.innerHTML  = `<svg viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M6 24 L38 24 M28 14 L40 24 L28 34" stroke="#38bdf8" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>`;
    }
  }

  /* ── DOM elements created once ───────────────────────── */
  const highlight = document.createElement('div');
  highlight.className = 'pg-highlight';

  const tooltip = document.createElement('div');
  tooltip.className = 'pg-tooltip';

  const arrow = document.createElement('div');
  arrow.className = 'pg-arrow';

  [highlight, tooltip, arrow].forEach(el => document.body.appendChild(el));

  function hideAll() {
    highlight.style.display = 'none';
    tooltip.style.display   = 'none';
    arrow.style.display     = 'none';
  }
  hideAll();

  /* ── Form field steps ─────────────────────────────────── */
  // We identify fields by their label text or input name/type
  const formSteps = [
    {
      selector: 'input[name="name"], input[placeholder*="name" i], input[id*="name" i]',
      label: 'Your Name',
      tip: '👋 Start here! Enter your <strong>full name</strong> so we can personalise your demo.',
    },
    {
      selector: 'input[name="company"], input[placeholder*="company" i], input[id*="company" i]',
      label: 'Company Name',
      tip: '🏢 Enter your <strong>company or business name</strong>.',
    },
    {
      selector: 'input[name="website"], input[placeholder*="website" i], input[type="url"]',
      label: 'Company Website',
      tip: '🌐 Enter your <strong>website URL</strong> — we\'ll train your AI on it in ~25 seconds! No website? Tick the box below.',
    },
    {
      selector: 'input[type="email"], input[name="email"], input[placeholder*="email" i]',
      label: 'Email Address',
      tip: '📧 Enter your <strong>email address</strong> — we\'ll send you your demo link here.',
    },
    {
      selector: 'select, [role="combobox"], [class*="select"]',
      label: 'Type of Business',
      tip: '🏷️ Select your <strong>type of business</strong> from the dropdown.',
    },
    {
      selector: 'button[type="submit"], input[type="submit"], button[class*="submit" i]',
      label: 'Build My AI Receptionist',
      tip: '🚀 You\'re all set! Click <strong>"Build my AI Receptionist"</strong> and watch the magic happen in 25 seconds.',
    },
  ];

  let currentStep = 0;
  let formGuideActive = false;

  function findEl(selector) {
    // Try multiple selectors separated by comma
    const selectors = selector.split(',').map(s => s.trim());
    for (const s of selectors) {
      try {
        const el = document.querySelector(s);
        if (el) return el;
      } catch(e) {}
    }
    return null;
  }

  function showFormStep(stepIndex) {
    const step = formSteps[stepIndex];
    if (!step) { hideAll(); return; }

    const el = findEl(step.selector);
    if (!el) {
      // Skip missing fields
      showFormStep(stepIndex + 1);
      return;
    }

    el.scrollIntoView({ behavior: 'smooth', block: 'center' });

    setTimeout(() => {
      highlight.style.display = 'block';
      tooltip.style.display   = 'block';
      arrow.style.display     = 'flex';

      positionHighlight(el);
      positionTooltip(el, window.innerWidth > 600 ? 'right' : 'bottom');
      positionArrow(el, 'down');

      const isLast = stepIndex === formSteps.length - 1;
      tooltip.innerHTML = `
        <div class="pg-step">Step ${stepIndex + 1} of ${formSteps.length}</div>
        <div class="pg-title">${step.label}</div>
        <div>${step.tip}</div>
        <button class="pg-btn" id="pg-next">${isLast ? '✅ Build It!' : 'Got it →'}</button>
        <button class="pg-btn-skip" id="pg-skip">Skip guide</button>
      `;

      document.getElementById('pg-next').onclick = () => {
        if (isLast) {
          hideAll();
          // Click the submit button
          el.click();
          watchForDemoScreen();
        } else {
          showFormStep(stepIndex + 1);
        }
      };
      document.getElementById('pg-skip').onclick = () => {
        hideAll();
        formGuideActive = false;
      };
    }, 400);
  }

  /* ── Watch for the demo (phone mockup) screen ─────────── */
  function watchForDemoScreen() {
    // Poll until the phone/demo screen appears
    const timer = setInterval(() => {
      // Look for "Chat with Voice" button or mic button or the phone mockup
      const chatBtn = findEl(
        '[class*="chat" i], [class*="voice" i], [class*="mic" i], ' +
        'button[class*="call" i], [data-testid*="voice"], ' +
        '[class*="phone" i] button, [class*="demo" i] button'
      );
      if (chatBtn) {
        clearInterval(timer);
        setTimeout(() => showCallGuide(chatBtn), 800);
      }
    }, 1000);
    // Give up after 90 seconds
    setTimeout(() => clearInterval(timer), 90000);
  }

  /* ── Call button guide ────────────────────────────────── */
  function showCallGuide(callBtn) {
    callBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });

    setTimeout(() => {
      highlight.style.display = 'block';
      tooltip.style.display   = 'block';
      arrow.style.display     = 'flex';

      positionHighlight(callBtn, 16);
      positionTooltip(callBtn, 'bottom');
      positionArrow(callBtn, 'down');

      // Add pulsing ring
      const ring = document.createElement('div');
      ring.className = 'pg-pulse-ring';
      const r = getRect(callBtn);
      const size = Math.max(r.width, r.height) + 20;
      ring.style.cssText = `width:${size}px;height:${size}px;top:${r.top + r.height/2 - size/2}px;left:${r.left + r.width/2 - size/2}px`;
      document.body.appendChild(ring);

      tooltip.innerHTML = `
        <div class="pg-title">🎙️ Talk to Your AI Receptionist!</div>
        <div>Tap <strong>"Chat with Voice"</strong>, allow microphone access, then say hello!<br><br>Ask it anything about your business — it already knows everything!</div>
        <button class="pg-btn" id="pg-call-ok">Let's Talk! 🚀</button>
        <button class="pg-btn-skip" id="pg-call-skip">Skip</button>
      `;

      document.getElementById('pg-call-ok').onclick = () => {
        hideAll();
        ring.remove();
        callBtn.click();
        watchForCallEnd();
      };
      document.getElementById('pg-call-skip').onclick = () => {
        hideAll();
        ring.remove();
        watchForCallEnd();
      };
    }, 400);
  }

  /* ── Watch for call end → show appointment banner ─────── */
  function watchForCallEnd() {
    // Watch for session-ended notice or "Start again" button
    const timer = setInterval(() => {
      const endNotice = findEl(
        '[class*="session" i], [class*="ended" i], ' +
        'button[class*="again" i], [class*="after-hours" i], ' +
        '[class*="book" i][class*="appoint" i]'
      );
      if (endNotice) {
        clearInterval(timer);
        setTimeout(showAppointmentBanner, 1500);
      }
    }, 1500);
    setTimeout(() => clearInterval(timer), 300000); // 5 min max
  }

  /* ── Appointment reminder banner ──────────────────────── */
  function showAppointmentBanner() {
    // Also highlight any "Book an appointment" button
    const bookBtn = findEl(
      '[class*="book" i], button[class*="appoint" i], ' +
      'a[href*="book" i], a[href*="appoint" i]'
    );

    if (bookBtn) {
      bookBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
      positionHighlight(bookBtn, 12);
      highlight.style.display = 'block';
      positionArrow(bookBtn, 'down');
      arrow.style.display = 'flex';
    }

    const banner = document.createElement('div');
    banner.className = 'pg-appt-banner';
    banner.innerHTML = `
      <button class="pg-close-banner" id="pg-close-appt">×</button>
      <div class="pg-appt-icon">📅</div>
      <div class="pg-appt-text">
        <strong>Don't miss out — Book a Free Consultation!</strong>
        Want to get this AI Receptionist for your business? Book a quick call with our team — it's free!
      </div>
      <button class="pg-appt-btn" id="pg-appt-book">Book Now →</button>
    `;
    document.body.appendChild(banner);

    document.getElementById('pg-close-appt').onclick = () => {
      banner.remove();
      hideAll();
    };
    document.getElementById('pg-appt-book').onclick = () => {
      banner.remove();
      hideAll();
      // Click the actual book button or open booking link
      if (bookBtn) {
        bookBtn.click();
      } else {
        window.open('https://links.pragna.ai/widget/bookings/srinivas-reddy-personal-calendar-e6vsub-lv', '_blank');
      }
    };
  }

  /* ── Entry point: start guide when form is visible ────── */
  function startGuide() {
    formGuideActive = true;
    // Show intro tooltip over the whole form first
    const form = findEl('form, [class*="form" i], [class*="signup" i]');
    if (!form) {
      showFormStep(0);
      return;
    }

    form.scrollIntoView({ behavior: 'smooth', block: 'center' });

    setTimeout(() => {
      highlight.style.display = 'block';
      tooltip.style.display   = 'block';
      arrow.style.display     = 'none';

      positionHighlight(form, 20);
      positionTooltip(form, window.innerWidth > 600 ? 'right' : 'bottom');

      tooltip.innerHTML = `
        <div class="pg-title">👋 Welcome to Pragna AI Demo!</div>
        <div>In just <strong>3 simple steps</strong> you'll hear your own AI Receptionist talking about your business:<br><br>
        1️⃣ Fill in a quick form<br>
        2️⃣ Your AI is built in ~25 seconds<br>
        3️⃣ Talk to it live!<br><br>
        Let's guide you through it step by step.</div>
        <button class="pg-btn" id="pg-intro-next">Let's Start! →</button>
        <button class="pg-btn-skip" id="pg-intro-skip">I'll do it myself</button>
      `;

      document.getElementById('pg-intro-next').onclick = () => showFormStep(0);
      document.getElementById('pg-intro-skip').onclick = () => {
        hideAll();
        formGuideActive = false;
      };
    }, 600);
  }

  /* ── Auto-start after short delay ────────────────────── */
  window.addEventListener('load', () => {
    setTimeout(startGuide, 1500);
  });

  /* Expose globally so it can be triggered manually */
  window.pragnaStartGuide = startGuide;

})();
