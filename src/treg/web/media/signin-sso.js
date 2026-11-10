/* One company sign-in control for the public pages' existing dialogs.
   Email finds a route; the WorkOS callback, never this lookup, authenticates the person. */
document.addEventListener('DOMContentLoaded', () => {
  const icons = {
    mail: '/media/signin/mail.svg',
    sso: '/media/signin/shield.svg',
    back: '/media/signin/arrow-left.svg',
    github: '/media/recruit/github.svg',
    google: '/media/aeo/logos/google.svg',
  };
  const style = document.createElement('style');
  style.textContent = '.treg-signin-method{display:flex;align-items:center;justify-content:center;gap:9px}' +
    '.treg-signin-method::before{content:"";display:block;width:17px;height:17px;flex:none;background:var(--treg-signin-icon) center/contain no-repeat}' +
    '.treg-signin-method.dark::before,.treg-signin-method.candy::before{filter:invert(1)}';
  document.head.append(style);
  const decorate = (element, name) => {
    element.classList.add('treg-signin-method');
    element.style.setProperty('--treg-signin-icon', `url("${icons[name]}")`);
  };
  const social = (modal) => {
    for (const name of ['github', 'google']) {
      const link = modal.querySelector(`a[href="/auth/${name}"]`);
      if (!link) continue;
      link.textContent = name === 'github' ? 'GitHub' : 'Google';
      decorate(link, name);
    }
  };
  const discover = async (value) => {
    const response = await fetch('/auth/sso/discover', {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({email: value}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Company sign-in is unavailable.');
    return data.teams || [];
  };
  const start = (slug) => {
    location.href = `/auth/sso/${encodeURIComponent(slug)}/start`;
  };
  const noMatch = 'This email has no company SSO connection. Use another sign-in method or ask your IT admin for your company link.';

  // Astra has its own native dialog and email form. Keep its form handlers intact.
  const astra = document.querySelector('#account-dialog');
  if (astra) {
    const form = astra.querySelector('#email-start-form');
    const verify = astra.querySelector('#email-verify-form');
    const email = astra.querySelector('#signin-email');
    const provider = astra.querySelector('a.button.dark[href^="/auth/"]');
    const divider = astra.querySelector('.signin-divider');
    if (!form || !verify || !email || !provider || !divider) return;
    astra.insertBefore(form, provider);
    astra.insertBefore(verify, provider);
    astra.insertBefore(divider, provider);
    divider.textContent = 'or sign in with';
    provider.classList.remove('dark');
    social(astra);
    form.querySelector('label').classList.remove('sr-only');
    form.querySelector('label').textContent = 'Email';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'button';
    button.textContent = 'Company SSO';
    divider.after(button);
    const codeButton = form.querySelector('#email-start-button');
    codeButton.classList.add('dark');
    decorate(codeButton, 'mail');
    decorate(astra.querySelector('#email-verify-button'), 'mail');
    decorate(button, 'sso');
    const backButton = document.createElement('button');
    backButton.type = 'button';
    backButton.className = 'button';
    backButton.textContent = 'Email sign-in code';
    decorate(backButton, 'back');
    backButton.hidden = true;
    divider.after(backButton);
    const setMode = (sso) => {
      codeButton.hidden = sso;
      codeButton.style.display = sso ? 'none' : '';
      button.classList.toggle('dark', sso);
      button.textContent = sso ? 'Continue with SSO' : 'Company SSO';
      (sso ? form : divider.parentNode).insertBefore(button, sso ? codeButton : backButton);
      backButton.hidden = !sso;
      backButton.style.display = sso ? '' : 'none';
      result.replaceChildren();
      email.focus();
    };
    const result = document.createElement('div');
    result.setAttribute('role', 'status');
    result.style.cssText = 'text-align:left;font-size:12px;line-height:1.5;margin-top:8px';
    form.append(result);
    backButton.style.display = 'none';
    email.addEventListener('input', () => result.replaceChildren());
    backButton.addEventListener('click', () => setMode(false));
    form.addEventListener('submit', (event) => {
      if (!codeButton.hidden) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      button.click();
    }, true);
    button.addEventListener('click', async () => {
      if (!codeButton.hidden) { setMode(true); return; }
      result.replaceChildren();
      const value = email.value.trim();
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) {
        result.textContent = 'Enter your work email first.';
        email.focus();
        return;
      }
      button.disabled = true;
      button.textContent = 'Finding company…';
      try {
        const teams = await discover(value);
        if (teams.length === 1) { start(teams[0].slug); return; }
        if (!teams.length) { result.textContent = noMatch; return; }
        result.textContent = 'Choose your company team:';
        for (const team of teams) {
          const choice = document.createElement('button');
          choice.type = 'button';
          choice.className = 'button';
          choice.textContent = `${team.name} · ${team.slug}`;
          choice.addEventListener('click', () => start(team.slug));
          result.append(choice);
        }
      } catch (cause) {
        result.textContent = cause.message || 'Company sign-in is temporarily unavailable.';
      } finally {
        button.disabled = false;
        button.textContent = 'Continue with SSO';
      }
    });
    return;
  }

  const modal = document.querySelector('#signin-scrim .smodal');
  const email = modal?.querySelector('#em-input');
  const emailStep = modal?.querySelector('#em-step1');
  const codeStep = modal?.querySelector('#em-step2');
  const error = modal?.querySelector('#em-err');
  const codeButton = modal?.querySelector('#em-btn1');
  const provider = modal?.querySelector('a[href="/auth/github"], a[href="/auth/google"]');
  if (!email || !emailStep || !codeStep || !error || !codeButton || !provider) return;

  // Put the email code first on every page, while preserving each page's existing
  // email-code, Google, GitHub, captcha and post-login handlers.
  const label = document.createElement('label');
  label.htmlFor = 'em-input';
  label.textContent = 'Email';
  label.style.cssText = 'display:block;text-align:left;font-size:12px;margin-top:14px';
  emailStep.insertBefore(label, email);
  modal.insertBefore(emailStep, provider);
  modal.insertBefore(codeStep, provider);
  modal.insertBefore(error, provider);
  // Use each page's existing primary and secondary button styles. Public pages use
  // different class names, so do not introduce a landing-only class on every page.
  const primaryClass = provider.className;
  const secondaryClass = primaryClass === 'candy'
    ? 'ghostbtn' : primaryClass.replace(/\bdark\b/g, '').trim();
  provider.className = secondaryClass;
  codeButton.className = primaryClass;
  social(modal);
  decorate(codeButton, 'mail');
  decorate(modal.querySelector('#em-btn2'), 'mail');
  const divider = modal.querySelector('.div');
  if (divider) {
    divider.textContent = 'or sign in with';
    modal.insertBefore(divider, provider);
  }

  const ssoButton = document.createElement('button');
  ssoButton.type = 'button';
  ssoButton.className = secondaryClass;
  ssoButton.textContent = 'Company SSO';
  decorate(ssoButton, 'sso');
  ssoButton.style.width = '100%';
  const backButton = document.createElement('button');
  backButton.type = 'button';
  backButton.className = secondaryClass;
  backButton.textContent = 'Email sign-in code';
  decorate(backButton, 'back');
  backButton.style.width = '100%';
  backButton.hidden = true;
  backButton.style.display = 'none';
  modal.insertBefore(ssoButton, provider);
  modal.insertBefore(backButton, provider);
  const result = document.createElement('div');
  result.setAttribute('role', 'status');
  result.style.cssText = 'text-align:left;font-size:12px;line-height:1.5;margin-top:8px';
  emailStep.append(result);
  email.addEventListener('input', () => { result.replaceChildren(); });
  const setMode = (sso) => {
    codeButton.hidden = sso;
    codeButton.style.display = sso ? 'none' : '';
    ssoButton.className = (sso ? primaryClass : secondaryClass) + ' treg-signin-method';
    ssoButton.textContent = sso ? 'Continue with SSO' : 'Company SSO';
    (sso ? emailStep : modal).insertBefore(ssoButton, sso ? codeButton : backButton);
    backButton.hidden = !sso;
    backButton.style.display = sso ? '' : 'none';
    result.replaceChildren();
    error.textContent = '';
    email.focus();
  };
  backButton.addEventListener('click', () => setMode(false));
  email.addEventListener('keyup', (event) => {
    if (event.key !== 'Enter' || !codeButton.hidden) return;
    event.stopImmediatePropagation();
    ssoButton.click();
  }, true);

  ssoButton.addEventListener('click', async () => {
    if (!codeButton.hidden) { setMode(true); return; }
    const value = email.value.trim();
    result.replaceChildren();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) {
      result.textContent = 'Enter your work email first.';
      email.focus();
      return;
    }
    ssoButton.disabled = true;
    ssoButton.textContent = 'Finding company…';
    try {
      const teams = await discover(value);
      if (teams.length === 1) {
        start(teams[0].slug);
        return;
      }
      if (!teams.length) {
        result.textContent = noMatch;
        return;
      }
      result.textContent = 'Choose your company team:';
      for (const team of teams) {
        const choice = document.createElement('button');
        choice.type = 'button';
        choice.className = codeButton.className;
        choice.style.width = '100%';
        choice.textContent = `${team.name} · ${team.slug}`;
        choice.addEventListener('click', () => start(team.slug));
        result.append(choice);
      }
    } catch (cause) {
      result.textContent = cause.message || 'Company sign-in is temporarily unavailable.';
    } finally {
      ssoButton.disabled = false;
      ssoButton.textContent = 'Continue with SSO';
    }
  });
});
