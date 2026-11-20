/* ============================================================
   Pragna AI Demo – Guided Overlay  v2
   ============================================================ */

(function () {
  /* ── Styles ───────────────────────────────────────────── */
  const css = `
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
      font-weight: 700; font-size: 16px; margin-bottom: 6px; color: #38bdf8;
    }
    .pg-tooltip .pg-step {
      font-size: 12px; opacity: 0.6; margin-bottom: 4px;
    }
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
    .pg-btn {
      display: inline-block; margin-top: 10px; padding: 8px 20px;
      background: #38bdf8; color: #0f172a; border: none; border-radius: 8px;
      font-weight: 700; font-size: 14px; cursor: pointer; transition: background 0.2s;
    }
    .pg-btn:hover { background: #7dd3fc; }
    .pg-btn-skip {
      display: inline-block; margin-top: 10px; margin-left: 10px;
      padding: 8px 14px; background: transparent; color: #94a3b8;
      border: 1px solid #334155; border-radius: 8px; font-size: 13px; cursor: pointer;
    }
    .pg-btn-skip:hover { color: #f8fafc; border-color: #64748b; }
    .pg-pulse-ring {
      position: fixed; border-radius: 50%; border: 3px solid #38bdf8;
      z-index: 9001; pointer-events: none;
      animation: pg-pulse 1.4s ease-out infinite;
    }
    @keyframes pg-pulse {
      0%   { transform: scale(1); opacity: 0.9; }
      100% { transform: scale(2.2); opacity: 0; }
    }
    .pg-appt-banner {
      position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
      background: #0f172a; border: 2px solid #38bdf8; border-radius: 16px;
      padding: 16px 24px; display: flex; align-items: center; gap: 14px;
      z-index: 9100; box-shadow: 0 8px 32px rgba(0,0,0,0.5);
      font-family: system-ui, sans-serif; max-width: 90vw;
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
    return el.getBoundingClientRect();
  }

  function clamp(val, min, max) {
    return Math.min(Math.max(val, min), max);
  }

  function positionHighlight(el, padding) {
    padding = padding || 10;
    const r = getRect(el);
    const top  = clamp(r.top  - padding, 4, window.innerHeight - 20);
    const left = clamp(r.left - padding, 4, window.innerWidth  - 20);
    highlight.style.top    = top + 'px';
    highlight.style.left   = left + 'px';
    highlight.style.width  = (r.width  + padding * 2) + 'px';
    highlight.style.height = (r.height + padding * 2) + 'px';
  }

  function positionTooltip(el, side) {
    side = side || 'right';
    const r = getRect(el);
    const tw = 290, margin = 16;
    let left, top;
    if (side === 'right') {
      left = r.left + r.width + margin;
      top  = r.top;
    } else if (side === 'bottom') {
      left = r.left;
      top  = r.top + r.height + margin;
    } else if (side === 'left') {
      left = r.left - tw - margin;
      top  = r.top;
    } else {
      left = r.left;
      top  = r.top - 180;
    }
    // Clamp so tooltip stays inside viewport
    left = clamp(left, 8, window.innerWidth  - tw - 8);
    top  = clamp(top,  8, window.innerHeight - 220);
    tooltip.style.left = left + 'px';
    tooltip.style.top  = top  + 'px';
  }

  function positionArrow(el, direction) {
    direction = direction || 'down';
    const r = getRect(el);
    arrow.style.display = 'flex';
    if (direction === 'down') {
      const arrowTop = clamp(r.top - 60, 4, window.innerHeight - 60);
      arrow.style.top  = arrowTop + 'px';
      arrow.style.left = clamp(r.left + r.width / 2 - 24, 4, window.innerWidth - 52) + 'px';
      arrow.innerHTML  = `<svg viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M24 6 L24 38 M14 28 L24 40 L34 28" stroke="#38bdf8" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>`;
    } else if (direction === 'right') {
      arrow.style.top  = clamp(r.top + r.height / 2 - 24, 4, window.innerHeight - 52) + 'px';
      arrow.style.left = clamp(r.left - 60, 4, window.innerWidth - 52) + 'px';
      arrow.innerHTML  = `<svg viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M6 24 L38 24 M28 14 L40 24 L28 34" stroke="#38bdf8" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>`;
    }
  }

  /* ── DOM elements ─────────────────────────────────────── */
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
  const formSteps = [
    {
      selector: '#name, input[name="name"]',
      label: 'Your Name',
      tip: '👋 Start here! Enter your <strong>full name</strong> so we can personalise your demo.',
    },
    {
      selector: '#company, input[name="company"]',
      label: 'Company Name',
      tip: '🏢 Enter your <strong>company or business name</strong>.',
    },
    {
      selector: '#website, input[name="website"]',
      label: 'Company Website',
      tip: '🌐 Enter your <strong>website URL</strong> — we\'ll train your AI on it in ~25 seconds! No website? Tick the checkbox below.',
    },
    {
      selector: '#email, input[name="email"]',
      label: 'Email Address',
      tip: '📧 Enter your <strong>email address</strong> — we\'ll send you your personalised demo link here.',
    },
    {
      selector: '#phone, input[name="phone"]',
      label: 'Phone Number',
      tip: '📱 Enter your <strong>phone number</strong>. Use the flag dropdown to select your country code first, then type your number.',
    },
    {
      selector: 'button[type="submit"], .submit-btn, [class*="submit" i] button',
      label: 'Build My AI Receptionist',
      tip: '🚀 You\'re all set! Click <strong>"Build my AI Receptionist"</strong> and in about 25 seconds your personalised AI will be ready to talk!',
    },
  ];

  let currentStep = 0;

  function findEl(selector) {
    const parts = selector.split(',').map(s => s.trim());
    for (const s of parts) {
      try {
        const el = document.querySelector(s);
        if (el) return el;
      } catch (e) {}
    }
    return null;
  }

  function showFormStep(stepIndex) {
    const step = formSteps[stepIndex];
    if (!step) { hideAll(); return; }

    const el = findEl(step.selector);
    if (!el) {
      showFormStep(stepIndex + 1);
      return;
    }

    el.scrollIntoView({ behavior: 'smooth', block: 'center' });

    setTimeout(function () {
      highlight.style.display = 'block';
      tooltip.style.display   = 'block';

      positionHighlight(el);
      positionTooltip(el, window.innerWidth > 600 ? 'right' : 'bottom');

      const r = getRect(el);
      // Only show arrow if there's room above the element
      if (r.top > 70) {
        arrow.style.display = 'flex';
        positionArrow(el, 'down');
      } else {
        arrow.style.display = 'none';
      }

      const isLast = stepIndex === formSteps.length - 1;
      tooltip.innerHTML = `
        <div class="pg-step">Step ${stepIndex + 1} of ${formSteps.length}</div>
        <div class="pg-title">${step.label}</div>
        <div>${step.tip}</div>
        <button class="pg-btn" id="pg-next">${isLast ? '✅ Build It!' : 'Got it →'}</button>
        <button class="pg-btn-skip" id="pg-skip">Skip guide</button>
      `;

      document.getElementById('pg-next').onclick = function () {
        if (isLast) {
          hideAll();
          el.click();
          watchForDemoScreen();
        } else {
          showFormStep(stepIndex + 1);
        }
      };
      document.getElementById('pg-skip').onclick = function () {
        hideAll();
      };
    }, 500);
  }

  /* ── Intro tooltip (centered, no form highlight) ──────── */
  function startGuide() {
    // Show a fixed-center intro before stepping through fields
    highlight.style.display = 'none';
    arrow.style.display     = 'none';
    tooltip.style.display   = 'block';

    // Center the tooltip on screen
    tooltip.style.left = '50%';
    tooltip.style.top  = '50%';
    tooltip.style.transform = 'translate(-50%, -50%)';

    tooltip.innerHTML = `
      <div class="pg-title">👋 Welcome to Pragna AI Demo!</div>
      <div>In just <strong>3 simple steps</strong> you'll hear your own AI Receptionist talking about your business:<br><br>
      1️⃣ Fill in the quick form<br>
      2️⃣ Your AI is built in ~25 seconds<br>
      3️⃣ Talk to it live!<br><br>
      Let us guide you through it step by step.</div>
      <button class="pg-btn" id="pg-intro-next">Let's Start! →</button>
      <button class="pg-btn-skip" id="pg-intro-skip">I'll do it myself</button>
    `;

    document.getElementById('pg-intro-next').onclick = function () {
      tooltip.style.transform = '';
      showFormStep(0);
    };
    document.getElementById('pg-intro-skip').onclick = function () {
      tooltip.style.transform = '';
      hideAll();
    };
  }

  /* ── Watch for the demo (phone mockup) screen ─────────── */
  function watchForDemoScreen() {
    var timer = setInterval(function () {
      var chatBtn = findEl(
        '[class*="voice" i] button, [class*="chat" i] button, ' +
        'button[class*="call" i], button[class*="voice" i], ' +
        '[class*="phone" i] button, [class*="demo" i] button, ' +
        '[data-testid*="voice"], [class*="mic" i]'
      );
      if (chatBtn) {
        clearInterval(timer);
        setTimeout(function () { showCallGuide(chatBtn); }, 800);
      }
    }, 1000);
    setTimeout(function () { clearInterval(timer); }, 90000);
  }

  /* ── Call button guide ────────────────────────────────── */
  function showCallGuide(callBtn) {
    callBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });

    setTimeout(function () {
      highlight.style.display = 'block';
      tooltip.style.display   = 'block';

      positionHighlight(callBtn, 16);
      positionTooltip(callBtn, 'bottom');

      const r = getRect(callBtn);
      if (r.top > 70) {
        arrow.style.display = 'flex';
        positionArrow(callBtn, 'down');
      } else {
        arrow.style.display = 'none';
      }

      var ring = document.createElement('div');
      ring.className = 'pg-pulse-ring';
      var cr = getRect(callBtn);
      var size = Math.max(cr.width, cr.height) + 20;
      ring.style.cssText = 'width:' + size + 'px;height:' + size + 'px;top:' + (cr.top + cr.height/2 - size/2) + 'px;left:' + (cr.left + cr.width/2 - size/2) + 'px';
      document.body.appendChild(ring);

      tooltip.innerHTML = `
        <div class="pg-title">🎙️ Talk to Your AI!</div>
        <div>Tap <strong>"Chat with Voice"</strong>, allow microphone access, then say hello!<br><br>Ask anything about your business — your AI already knows everything!</div>
        <button class="pg-btn" id="pg-call-ok">Let's Talk! 🚀</button>
        <button class="pg-btn-skip" id="pg-call-skip">Skip</button>
      `;

      document.getElementById('pg-call-ok').onclick = function () {
        hideAll(); ring.remove(); callBtn.click(); watchForCallEnd();
      };
      document.getElementById('pg-call-skip').onclick = function () {
        hideAll(); ring.remove(); watchForCallEnd();
      };
    }, 500);
  }

  /* ── Watch for call end → show appointment banner ─────── */
  function watchForCallEnd() {
    var timer = setInterval(function () {
      var endNotice = findEl(
        '[class*="session" i], [class*="ended" i], ' +
        'button[class*="again" i], [class*="after-hours" i]'
      );
      if (endNotice) {
        clearInterval(timer);
        setTimeout(showAppointmentBanner, 1500);
      }
    }, 1500);
    setTimeout(function () { clearInterval(timer); }, 300000);
  }

  /* ── Appointment banner ───────────────────────────────── */
  function showAppointmentBanner() {
    var bookBtn = findEl('[class*="book" i], button[class*="appoint" i], a[href*="book" i], a[href*="appoint" i]');
    if (bookBtn) {
      bookBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
      positionHighlight(bookBtn, 12);
      highlight.style.display = 'block';
      var r = getRect(bookBtn);
      if (r.top > 70) { arrow.style.display = 'flex'; positionArrow(bookBtn, 'down'); }
    }

    var banner = document.createElement('div');
    banner.className = 'pg-appt-banner';
    banner.innerHTML = `
      <button class="pg-close-banner" id="pg-close-appt">×</button>
      <div class="pg-appt-icon">📅</div>
      <div class="pg-appt-text">
        <strong>Book a Free Consultation!</strong>
        Want this AI for your business? Book a quick call with our team — it's free!
      </div>
      <button class="pg-appt-btn" id="pg-appt-book">Book Now →</button>
    `;
    document.body.appendChild(banner);

    document.getElementById('pg-close-appt').onclick = function () { banner.remove(); hideAll(); };
    document.getElementById('pg-appt-book').onclick = function () {
      banner.remove(); hideAll();
      if (bookBtn) { bookBtn.click(); }
      else { window.open('https://links.pragna.ai/widget/bookings/srinivas-reddy-personal-calendar-e6vsub-lv', '_blank'); }
    };
  }

  /* ── Auto-start ───────────────────────────────────────── */
  function tryStart() {
    setTimeout(startGuide, 1500);
  }

  if (document.readyState === 'complete') {
    tryStart();
  } else {
    window.addEventListener('load', tryStart);
  }

  window.pragnaStartGuide = startGuide;

})();
