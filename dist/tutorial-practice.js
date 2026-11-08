async function loadTutorialChart() {
  const chart = document.getElementById('tutorial-chart');
  if (!chart) return;
  try {
    const data = await getPreviewCase();
    chart.innerHTML = chartSvg(data.chart, { mode: 'preview' });
    chart.setAttribute('aria-label', `Проверенный исторический график за 60 торговых дней, ${data.start_date} — ${data.decision_date}`);
  } catch {
    chart.textContent = 'Проверенный график временно недоступен.';
  }
}

async function tutorialPracticeView() {
  root.innerHTML = shell('<main class="tutorial-layout"><div class="loading">Загружаем учебный сценарий…</div></main>');
  bindNav();
  try {
    const payload = await api('/api/tutorial/practice');
    const round = payload.round;
    const companies = round.companies.map(company => `<option value="${company.ticker}">${esc(company.name)} · ${esc(company.sector)}</option>`).join('');
    const years = [...new Set((round.periods || []).map(period => period[0]))].sort();
    const factors = round.factors.map((factor, index) => `<label class="factor"><input type="checkbox" name="factor" form="tutorial-practice-form" value="${index}"><span><b>${esc(factor.name)}</b><small>${esc(factor.description)}</small></span></label>`).join('');
    const quiz = round.quiz.map((question, index) => `<fieldset class="quiz-question"><legend>${index + 1}. ${esc(question.question)}</legend>${question.options.map((option, optionIndex) => `<label><input type="radio" name="quiz-${question.id}" value="${optionIndex}" required><span>${esc(option)}</span></label>`).join('')}</fieldset>`).join('');
    root.innerHTML = shell(`<main class="tutorial-layout"><div class="tutorial-head"><div class="eyebrow">Обязательный вводный урок · 04 / 04</div><div class="tutorial-progress"><span class="active"></span><span class="active"></span><span class="active"></span><span class="active"></span></div></div><section class="practice-head"><div><div class="eyebrow">Пробный раунд · исторический пример</div><h1>${esc(round.title)}</h1><p>Изучите реальные 60 торговых дней. Заполните прогноз, стратегию и мини-тест; продолжение графика откроется после фиксации.</p></div></section><section class="tutorial-practice-grid"><article class="panel tutorial-practice-context"><div class="panel-head"><div><h2>Динамика неизвестной акции</h2><div class="panel-sub">Цена относительно точки решения · 100</div></div><span class="tag">60 торговых наблюдений</span></div><div class="chart-wrap">${chartSvg(round.chart)}</div><div class="tutorial-context-copy"><span>Событие</span><p>${esc(round.briefing.event)}</p><span>Контекст</span><p>${esc(round.briefing.context)}</p></div><div class="section-label">Какие факторы важны?</div><div class="factor-list">${factors}</div></article><form class="panel form-panel tutorial-practice-form" id="tutorial-practice-form"><div class="form-section"><div class="section-label">01 · Ваша версия</div><label class="field-label">Компания<select class="field" name="company" required><option value="">Выберите компанию…</option>${companies}</select></label><div class="field-grid"><label class="field-label">Месяц<select class="field" name="month" required><option value="">Сначала выберите год</option></select></label><label class="field-label">Год<select class="field" name="year" required><option value="">Год…</option>${years.map(year => `<option>${year}</option>`).join('')}</select></label></div></div><div class="form-section"><div class="section-label">02 · Прогноз на следующие 30 дней</div><div class="choice-row">${[['up', 'Рост'], ['flat', 'Стабильность'], ['down', 'Падение']].map(([value, title]) => `<div class="choice"><input type="radio" id="practice-f-${value}" name="forecast" value="${value}" required><label for="practice-f-${value}"><span>${title}</span></label></div>`).join('')}</div><label class="field-label">Почему вы так думаете?<textarea class="textarea" name="rationale" minlength="5" maxlength="500" required placeholder="Назовите факторы и степень уверенности…"></textarea></label></div><div class="form-section"><div class="section-label">03 · Инвестиционная стратегия</div><div class="choice-row">${[['buy', 'Купить'], ['hold', 'Удерживать'], ['sell', 'Продать']].map(([value, title]) => `<div class="choice"><input type="radio" id="practice-s-${value}" name="strategy" value="${value}" required><label for="practice-s-${value}"><span>${title}</span></label></div>`).join('')}</div></div><div class="form-section"><div class="section-label">04 · Проверка понимания</div>${quiz}</div><div class="submit-row"><button class="primary" type="submit" id="tutorial-practice-submit">Показать разбор ${icons.arrow}</button><div class="form-hint">Учебный ответ не попадёт в статистику профиля.</div><div class="error" id="tutorial-practice-error" role="alert" hidden></div></div></form></section></main>`);
    bindNav();
    const form = document.getElementById('tutorial-practice-form');
    const yearSelect = form.elements.year, monthSelect = form.elements.month;
    const updatePracticeMonths = () => {
      const months = (round.periods || []).filter(period => String(period[0]) === yearSelect.value).map(period => Number(period[1])).sort((a, b) => a - b);
      monthSelect.innerHTML = `<option value="">Выберите месяц…</option>${months.map(month => `<option value="${month}">${MONTHS[month - 1]}</option>`).join('')}`;
    };
    yearSelect.addEventListener('change', updatePracticeMonths);
    form.onsubmit = async event => {
      event.preventDefault();
      const data = new FormData(form);
      const errorBox = document.getElementById('tutorial-practice-error');
      const button = document.getElementById('tutorial-practice-submit');
      const selectedFactors = data.getAll('factor');
      if (!selectedFactors.length) {
        errorBox.textContent = 'Выберите хотя бы один важный фактор.';
        errorBox.hidden = false;
        form.querySelector('.factor input')?.focus();
        return;
      }
      button.disabled = true;
      button.textContent = 'Подготавливаем разбор…';
      errorBox.hidden = true;
      const answer = {
        company: data.get('company'), year: data.get('year'), month: data.get('month'),
        forecast: data.get('forecast'), rationale: data.get('rationale'), strategy: data.get('strategy'),
        factors: selectedFactors.map(Number),
        quiz_answers: round.quiz.map(question => ({ id: question.id, option: Number(data.get(`quiz-${question.id}`)) })),
      };
      try {
        const response = await api('/api/tutorial/practice', { method: 'POST', body: JSON.stringify(answer) });
        showTutorialPracticeResult(response.result);
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
        button.disabled = false;
        button.innerHTML = `Показать разбор ${icons.arrow}`;
      }
    };
  } catch (error) {
    errorScreen(error.message);
  }
}

function showTutorialPracticeResult(result) {
  const points = result.reveal_prices.map(point => ({ value: point.close / result.reveal_prices[59].close * 100, date: point.date, close: point.close }));
  const forecastLabels = { up: 'Рост', flat: 'Без существенного изменения', down: 'Падение' };
  const strategyLabels = { buy: 'Купить', hold: 'Удерживать', sell: 'Продать' };
  root.innerHTML = shell(`<main class="tutorial-layout"><div class="tutorial-head"><div class="eyebrow">Пробный раунд · разбор</div><div class="tutorial-progress"><span class="active"></span><span class="active"></span><span class="active"></span><span class="active"></span></div></div><section class="panel tutorial-practice-result"><div class="eyebrow">Теперь видны следующие 30 сессий</div><h1>${esc(result.company_name)} · ${pct(result.change_pct)}</h1><div class="chart-wrap">${chartSvg(points, { mode: 'revealed', forecast: result.answer?.forecast })}</div><div class="chart-legend"><span><i class="legend-line actual"></i>Фактические котировки</span><span><i class="legend-line predicted"></i>Ваш прогноз</span></div><p>Фактическое изменение соответствовало сценарию «${forecastLabels[result.actual_forecast]}». Совпадение исхода с прогнозом не доказывает единственную причину движения.</p><div class="tutorial-result-grid"><div><span>Компания</span><b>${result.company_correct ? 'определена верно' : `это была ${esc(result.company_name)}`}</b></div><div><span>Прогноз</span><b>${result.forecast_correct ? 'направление совпало' : forecastLabels[result.actual_forecast]}</b></div><div><span>Стратегия</span><b>${strategyLabels[result.answer.strategy]}</b></div><div><span>Мини-тест</span><b>${result.quiz_result.correct} / ${result.quiz_result.total}</b></div></div><div class="factor-feedback"><div class="section-label">Выбранные факторы</div><p>${esc((result.factor_analysis?.selected || []).join(', '))}</p><p>${esc(result.factor_analysis?.interpretation || 'Сопоставляйте несколько сигналов и сохраняйте неопределённость.')}</p><p><b>Каких данных не хватало:</b> ${esc(result.factor_analysis?.missing_data || '')}</p></div><div class="success-note">Учебный раунд не сохранён в профиле и не влияет на игровые показатели.</div><button class="primary" id="finish-tutorial">Завершить урок и открыть Easy ${icons.arrow}</button><div class="error" id="finish-tutorial-error" role="alert" hidden></div></section></main>`);
  bindNav();
  document.getElementById('finish-tutorial').onclick = async event => {
    const button = event.currentTarget;
    const errorBox = document.getElementById('finish-tutorial-error');
    button.disabled = true;
    button.textContent = 'Сохраняем прогресс…';
    try {
      const response = await api('/api/tutorial/progress', { method: 'POST', body: JSON.stringify({ step: 4, completed: true }) });
      user = response.user;
      dashboard();
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
      button.disabled = false;
      button.innerHTML = `Завершить урок и открыть Easy ${icons.arrow}`;
    }
  };
}
