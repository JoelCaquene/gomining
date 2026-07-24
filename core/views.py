from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, authenticate, logout, update_session_auth_hash
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum
from django.urls import reverse
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db import transaction
import random
from datetime import date, time, timedelta
from django.utils import timezone
from decimal import Decimal

from .forms import RegisterForm, DepositForm, WithdrawalForm, BankDetailsForm
from .models import PlatformSettings, CustomUser, Level, UserLevel, BankDetails, Deposit, Withdrawal, Task, PlatformBankDetails, Roulette, RouletteSettings

from .models import Post, PlatformSettings

# --- FUNÇÃO HOME ---
def home(request):
    if request.user.is_authenticated:
        return redirect('menu')
    return redirect('cadastro')

# --- FUNÇÃO MENU ---
@login_required
def menu(request):
    user = request.user
    active_levels = UserLevel.objects.filter(user=user, is_active=True)
    approved_deposit_total = Deposit.objects.filter(user=user, is_approved=True).aggregate(Sum('amount'))['amount__sum'] or 0
    today = date.today()
    daily_income = Task.objects.filter(user=user, completed_at__date=today).aggregate(Sum('earnings'))['earnings__sum'] or 0
    total_withdrawals = Withdrawal.objects.filter(user=user, status='Aprovado').aggregate(Sum('amount'))['amount__sum'] or 0

    try:
        platform_settings = PlatformSettings.objects.first()
        whatsapp_link = platform_settings.whatsapp_link
    except (PlatformSettings.DoesNotExist, AttributeError):
        whatsapp_link = '#'

    context = {
        'user': user,
        'has_active_plans': active_levels.exists(),
        'approved_deposit_total': approved_deposit_total,
        'daily_income': daily_income,
        'total_withdrawals': total_withdrawals,
        'whatsapp_link': whatsapp_link,
    }
    return render(request, 'menu.html', context)

# --- CADASTRO ---
def cadastro(request):
    invite_code_from_url = request.GET.get('invite', None)
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.set_password(form.cleaned_data['password'])
            user.available_balance = 0 
            
            invited_by_code = form.cleaned_data.get('invited_by_code')
            if invited_by_code:
                try:
                    invited_by_user = CustomUser.objects.get(invite_code=invited_by_code)
                    user.invited_by = invited_by_user
                except CustomUser.DoesNotExist:
                    messages.error(request, 'Código de convite inválido.')
                    return render(request, 'cadastro.html', {'form': form})
            
            user.save()
            login(request, user)
            messages.success(request, 'Cadastro realizado com sucesso!')
            return redirect('menu')
    else:
        form = RegisterForm(initial={'invited_by_code': invite_code_from_url}) if invite_code_from_url else RegisterForm()
    
    try:
        whatsapp_link = PlatformSettings.objects.first().whatsapp_link
    except (PlatformSettings.DoesNotExist, AttributeError):
        whatsapp_link = '#'
    return render(request, 'cadastro.html', {'form': form, 'whatsapp_link': whatsapp_link})

def user_login(request):
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect('menu')
    else:
        form = AuthenticationForm()
    try:
        whatsapp_link = PlatformSettings.objects.first().whatsapp_link
    except (PlatformSettings.DoesNotExist, AttributeError):
        whatsapp_link = '#'
    return render(request, 'login.html', {'form': form, 'whatsapp_link': whatsapp_link})

@login_required
def user_logout(request):
    logout(request)
    return redirect('menu')

# --- DEPÓSITO ---
@login_required
def deposito(request):
    platform_bank_details = PlatformBankDetails.objects.all()
    deposit_instruction = PlatformSettings.objects.first().deposit_instruction if PlatformSettings.objects.first() else 'Instruções de depósito não disponíveis.'
    level_deposits = Level.objects.all().values_list('deposit_value', flat=True).distinct().order_by('deposit_value')
    level_deposits_list = [str(d) for d in level_deposits] 

    if request.method == 'POST':
        form = DepositForm(request.POST, request.FILES)
        if form.is_valid():
            deposit = form.save(commit=False)
            deposit.user = request.user
            deposit.save()
            return render(request, 'deposito.html', {
                'platform_bank_details': platform_bank_details,
                'deposit_instruction': deposit_instruction,
                'level_deposits_list': level_deposits_list,
                'deposit_success': True 
            })
        else:
            messages.error(request, 'Erro ao enviar o depósito.')
    
    form = DepositForm()
    context = {
        'platform_bank_details': platform_bank_details,
        'deposit_instruction': deposit_instruction,
        'form': form,
        'level_deposits_list': level_deposits_list,
        'deposit_success': False,
    }
    return render(request, 'deposito.html', context)

@login_required
def approve_deposit(request, deposit_id):
    if not request.user.is_staff:
        return redirect('menu')
    deposit = get_object_or_404(Deposit, id=deposit_id)
    if not deposit.is_approved:
        deposit.is_approved = True
        deposit.save()
        deposit.user.available_balance += deposit.amount
        deposit.user.save()
        messages.success(request, 'Depósito aprovado.')
    return redirect('renda')

# --- SAQUE ---
@login_required
def saque(request):
    MIN_WITHDRAWAL_AMOUNT = 2500
    START_TIME = time(9, 0, 0)
    END_TIME = time(17, 0, 0)
    withdrawal_instruction = PlatformSettings.objects.first().withdrawal_instruction if PlatformSettings.objects.first() else ''
    withdrawal_records = Withdrawal.objects.filter(user=request.user).order_by('-created_at')
    has_bank_details = BankDetails.objects.filter(user=request.user).exists()
    now = timezone.localtime(timezone.now()).time()
    today = timezone.localdate(timezone.now())
    is_time_to_withdraw = START_TIME <= now <= END_TIME
    withdrawals_today_count = Withdrawal.objects.filter(user=request.user, created_at__date=today, status__in=['Pendente', 'Aprovado']).count()
    can_withdraw_today = withdrawals_today_count == 0
    
    if request.method == 'POST':
        form = WithdrawalForm(request.POST)
        if form.is_valid():
            amount = form.cleaned_data['amount']
            if not can_withdraw_today:
                messages.error(request, 'Apenas 1 saque por dia.')
            elif not is_time_to_withdraw:
                messages.error(request, 'Fora do horário de saque.')
            elif not has_bank_details:
                messages.error(request, 'Adicione coordenadas bancárias.')
            elif amount < MIN_WITHDRAWAL_AMOUNT:
                messages.error(request, 'Valor mínimo insuficiente.')
            elif request.user.available_balance < amount:
                messages.error(request, 'Saldo insuficiente.')
            else:
                Withdrawal.objects.create(user=request.user, amount=amount)
                request.user.available_balance -= amount
                request.user.save()
                messages.success(request, 'Saque solicitado.')
                return redirect('saque')
    else:
        form = WithdrawalForm()

    context = {
        'withdrawal_instruction': withdrawal_instruction,
        'withdrawal_records': withdrawal_records,
        'form': form,
        'has_bank_details': has_bank_details,
        'is_time_to_withdraw': is_time_to_withdraw,
        'MIN_WITHDRAWAL_AMOUNT': MIN_WITHDRAWAL_AMOUNT,
        'can_withdraw_today': can_withdraw_today,
    }
    return render(request, 'saque.html', context)

# --- AUXILIAR DE PROCESSAMENTO PROATIVO EM TEMPO REAL ---
def _proactively_update_user_level(ul, user):
    """Calcula e liquida os saldos passados de forma real e matemática baseada no tempo cronológico."""
    if not ul.is_active:
        return
    
    lvl = ul.level
    last_run = ul.last_task_processed_at if ul.last_task_processed_at else ul.activated_at
    now = timezone.now()
    time_elapsed = now - last_run
    
    # Quantidade de ciclos inteiros de 24 horas acumulados em background
    intervals_passed = int(time_elapsed.total_seconds() // 86400)
    
    if intervals_passed > 0:
        daily_rate = Decimal(str(lvl.daily_gain))
        
        # Não permite processar mais do que os dias restantes do ciclo
        remaining_days = lvl.cycle_days - ul.days_processed
        actual_processing_days = min(intervals_passed, remaining_days)
        
        if actual_processing_days > 0:
            for _ in range(actual_processing_days):
                ul.days_processed += 1
                Task.objects.create(user=user, user_level=ul, earnings=daily_rate)
                
                if lvl.category == 'short_term':
                    user.available_balance += daily_rate
                    ul.accumulated_credit += daily_rate
                elif lvl.category in ['long_term', 'activities']:
                    ul.accumulated_credit += daily_rate
            
            # Ajusta o ponto de ancoragem do tempo real proporcionalmente aos dias liquidados
            ul.last_task_processed_at = last_run + timedelta(days=actual_processing_days)
            
            if ul.days_processed >= lvl.cycle_days:
                ul.is_active = False
                ul.cycle_completed = True
                if lvl.category in ['long_term', 'activities']:
                    user.available_balance += ul.accumulated_credit
                    ul.payout_executed = True
            
            ul.save()
            user.save()

# --- TAREFA (PAINEL DE TELEVISÃO / MONITORAMENTO CONFORME SOLICITADO) ---
@login_required
def tarefa(request):
    user = request.user
    user_levels = UserLevel.objects.filter(user=user, is_active=True).select_related('level')
    active_monitors = []
    
    for ul in user_levels:
        # Sincroniza o banco com o tempo cronológico decorrido antes de renderizar
        _proactively_update_user_level(ul, user)
        
        lvl = ul.level
        last_run = ul.last_task_processed_at if ul.last_task_processed_at else ul.activated_at
        time_elapsed = timezone.now() - last_run
        
        remaining_seconds = 86400 - time_elapsed.total_seconds()
        if remaining_seconds < 0:
            remaining_seconds = 0

        # Cálculo dinâmico do ganho visual em tempo real para Longo Prazo e Actividades
        current_display_profit = ul.accumulated_credit
        if ul.is_active and lvl.category in ['long_term', 'activities']:
            daily_rate = Decimal(str(lvl.daily_gain))
            percent_of_day = Decimal(str(min(time_elapsed.total_seconds() / 86400.0, 1.0)))
            current_display_profit += (daily_rate * percent_of_day)

        active_monitors.append({
            'user_level_id': ul.id,
            'name': lvl.name,
            'category': lvl.category,
            'category_display': lvl.get_category_display(),
            'deposit_value': lvl.deposit_value,
            'daily_gain': lvl.daily_gain,
            'cycle_days': lvl.cycle_days,
            'days_passed': ul.days_processed,
            'accumulated_profit': round(current_display_profit, 2),
            'total_potential_gain': lvl.total_expected_gain,
            'remaining_seconds': int(remaining_seconds),
            'is_active': ul.is_active,
            'is_locked': False
        })
        
    context = {
        'active_monitors': active_monitors,
        'has_active_plans': user_levels.filter(is_active=True).exists(),
    }
    return render(request, 'tarefa.html', context)

# --- MOTOR DE LIQUIDAÇÃO AUTOMÁTICA VIA REQUISIÇÃO (CHAMADA CONTROLE REAL) ---
@login_required
@require_POST
@transaction.atomic
def process_task(request):
    import json
    user = request.user
    
    try:
        data = json.loads(request.body)
        user_level_id = data.get('user_level_id')
        
        ul = get_object_or_404(UserLevel, id=user_level_id, user=user)
        
        # Executa a atualização baseada no tempo real decorrido
        _proactively_update_user_level(ul, user)
        
        lvl = ul.level
        last_run = ul.last_task_processed_at if ul.last_task_processed_at else ul.activated_at
        time_elapsed = timezone.now() - last_run
        tempo_restante = max(0, 86400 - time_elapsed.total_seconds())
        
        if not ul.is_active:
            return JsonResponse({
                'success': True,
                'message': 'Plano concluído com sucesso.',
                'days_processed': ul.days_processed,
                'accumulated_profit': float(ul.accumulated_credit),
                'is_active': False
            })

        return JsonResponse({
            'success': True,
            'message': 'Sincronizado com o servidor.',
            'days_processed': ul.days_processed,
            'accumulated_profit': float(ul.accumulated_credit),
            'remaining_seconds': int(tempo_restante),
            'is_active': ul.is_active
        })

    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Erro crítico no motor: {str(e)}'})

# --- MAPEAMENTO DA ROTA COMPATÍVEL COM O TEMPLATE MONITOR.HTML ---
@login_required
@require_POST
def process_level_task(request):
    return process_task(request)

# --- GERENCIAMENTO DE NÍVEIS (COMPRA E EXIBIÇÃO DE MULTI-CARDS COMPLETA) ---
@login_required
@transaction.atomic
def nivel(request):
    user = request.user
    
    if request.method == 'POST':
        level_id = request.POST.get('level_id')
        level_to_buy = get_object_or_404(Level, id=level_id)
        val = level_to_buy.deposit_value

        active_count_this_level = UserLevel.objects.filter(user=user, level=level_to_buy, is_active=True).count()
        if active_count_this_level >= 3:
            messages.error(request, f'Bloqueado! Limite de 3 ativações simultâneas atingido para o plano {level_to_buy.get_category_display()} {level_to_buy.name}.')
            return redirect('nivel')

        # --- NOVA LÓGICA: CURTO PRAZO SEGUE O NÚMERO DE ATIVAÇÕES DO LONGO PRAZO ---
        if level_to_buy.category == 'short_term':
            long_term_count = UserLevel.objects.filter(
                user=user, 
                level__category='long_term', 
                level__name=level_to_buy.name
            ).count()
            
            short_term_count = UserLevel.objects.filter(
                user=user, 
                level=level_to_buy
            ).count()

            if short_term_count >= long_term_count:
                messages.error(request, f'Bloqueado! Ative mais vezes o plano de Longo Prazo {level_to_buy.name} para desbloquear novas ativações de curto prazo.')
                return redirect('nivel')
        # -------------------------------------------------------------------------

        active_long_term = UserLevel.objects.filter(user=user, is_active=True, level__category='long_term').values_list('level__name', flat=True)
        active_short_term = UserLevel.objects.filter(user=user, is_active=True, level__category='short_term').values_list('level__name', flat=True)

        if level_to_buy.category == 'short_term' and level_to_buy.name not in active_long_term:
            messages.error(request, f'Bloqueado! É obrigatório possuir o plano Longo Prazo {level_to_buy.name} Ativo.')
            return redirect('nivel')
                
        elif level_to_buy.category == 'activities':
            if level_to_buy.name not in active_long_term or level_to_buy.name not in active_short_term:
                messages.error(request, f'Bloqueado! Requer planos Longo e Curto Prazo do nível {level_to_buy.name} ativos.')
                return redirect('nivel')

        if user.available_balance >= val:
            user.available_balance -= val
            UserLevel.objects.create(user=user, level=level_to_buy, is_active=True, activated_at=timezone.now(), last_task_processed_at=timezone.now())
            user.level_active = True
            user.save()

            p1 = user.invited_by
            if p1 and UserLevel.objects.filter(user=p1, is_active=True).exists():
                com1 = val * Decimal('0.15')
                p1.available_balance += com1
                p1.subsidy_balance += com1
                p1.save()
                p2 = p1.invited_by
                if p2 and UserLevel.objects.filter(user=p2, is_active=True).exists():
                    com2 = val * Decimal('0.03')
                    p2.available_balance += com2
                    p2.subsidy_balance += com2
                    p2.save()
                    p3 = p2.invited_by
                    if p3 and UserLevel.objects.filter(user=p3, is_active=True).exists():
                        com3 = val * Decimal('0.01')
                        p3.available_balance += com3
                        p3.subsidy_balance += com3
                        p3.save()

            messages.success(request, f'Plano {level_to_buy.get_category_display()} {level_to_buy.name} ativado!')
        else:
            messages.error(request, 'Saldo insuficiente.')
        return redirect('nivel')

    user_active_long = list(UserLevel.objects.filter(user=user, is_active=True, level__category='long_term').values_list('level__name', flat=True))
    user_active_short = list(UserLevel.objects.filter(user=user, is_active=True, level__category='short_term').values_list('level__name', flat=True))
    all_levels = Level.objects.all().order_by('deposit_value')
    
    long_term_levels, short_term_levels, activity_levels = [], [], []

    for lvl in all_levels:
        user_instances = UserLevel.objects.filter(user=user, level=lvl, is_active=True)
        active_investments = []
        
        for inst in user_instances:
            _proactively_update_user_level(inst, user)
            last_run = inst.last_task_processed_at if inst.last_task_processed_at else inst.activated_at
            time_elapsed = timezone.now() - last_run
            rem_sec = max(0, 86400 - time_elapsed.total_seconds())
            
            active_investments.append({
                'user_level_id': inst.id,
                'days_passed': inst.days_processed,
                'accumulated_profit': inst.accumulated_credit,
                'remaining_seconds': int(rem_sec)
            })

        lvl_data = {
            'id': lvl.id,
            'name': lvl.name,
            'deposit_value': lvl.deposit_value,
            'daily_gain': lvl.daily_gain,
            'monthly_gain': lvl.monthly_gain,
            'annual_gain': lvl.annual_gain,
            'cycle_days': lvl.cycle_days,
            'total_gain': lvl.total_expected_gain,
            'is_active': user_instances.exists(),
            'active_count': user_instances.count(),
            'active_investments': active_investments,
            'is_locked': False
        }

        if lvl.category == 'long_term':
            long_term_levels.append(lvl_data)
        elif lvl.category == 'short_term':
            lvl_data['is_locked'] = lvl.name not in user_active_long
            short_term_levels.append(lvl_data)
        elif lvl.category == 'activities':
            lvl_data['is_locked'] = (lvl.name not in user_active_long) or (lvl.name not in user_active_short)
            activity_levels.append(lvl_data)

    context = {
        'long_term_levels': long_term_levels,
        'short_term_levels': short_term_levels,
        'activity_levels': activity_levels,
    }
    return render(request, 'nivel.html', context)
    
# --- EQUIPA ---
@login_required
def equipa(request):
    user = request.user
    level_a = CustomUser.objects.filter(invited_by=user)
    level_b = CustomUser.objects.filter(invited_by__in=level_a)
    level_c = CustomUser.objects.filter(invited_by__in=level_b)

    context = {
        'team_count': level_a.count() + level_b.count() + level_c.count(),
        'total_investors': (level_a.filter(userlevel__is_active=True).distinct().count() + 
                            level_b.filter(userlevel__is_active=True).distinct().count() + 
                            level_c.filter(userlevel__is_active=True).distinct().count()),
        'invite_link': request.build_absolute_uri(reverse('cadastro')) + f'?invite={user.invite_code}',
        'subsidy_balance': user.subsidy_balance,
        'level_a_count': level_a.count(),
        'level_a_investors': level_a.filter(userlevel__is_active=True).distinct().count(),
        'level_b_count': level_b.count(),
        'level_b_investors': level_b.filter(userlevel__is_active=True).distinct().count(),
        'level_c_count': level_c.count(),
        'level_c_investors': level_c.filter(userlevel__is_active=True).distinct().count(),
    }
    return render(request, 'equipa.html', context)

# --- ROLETA ---
@login_required
def roleta(request):
    user = request.user
    roulette_settings = RouletteSettings.objects.first()
    prizes_list = [p.strip() for p in roulette_settings.prizes.split(',')] if roulette_settings and roulette_settings.prizes else ['0', '500', '1000', '0', '5000', '200', '0', '10000']
    recent_winners = Roulette.objects.filter(is_approved=True).order_by('-spin_date')[:10]
    context = {'roulette_spins': user.roulette_spins, 'prizes_list': prizes_list, 'recent_winners': recent_winners}
    return render(request, 'roleta.html', context)

@login_required
@require_POST
def spin_roulette(request):
    user = request.user
    if not user.roulette_spins or user.roulette_spins <= 0:
        return JsonResponse({'success': False, 'message': 'Sem giros.'})

    roulette_settings = RouletteSettings.objects.first()
    prizes_raw = [p.strip() for p in roulette_settings.prizes.split(',')] if roulette_settings and roulette_settings.prizes else ['0', '500', '1000', '0', '5000', '200', '0', '10000']
    weighted_pool = []
    for p in prizes_raw:
        val = Decimal(p)
        if val == 0: weighted_pool.extend([p] * 10)
        elif val <= 500: weighted_pool.extend([p] * 5)
        else: weighted_pool.append(p)

    winning_prize_str = random.choice(weighted_pool)
    prize_amount = Decimal(winning_prize_str)
    user.roulette_spins -= 1
    user.subsidy_balance += prize_amount
    user.available_balance += prize_amount
    user.save()
    Roulette.objects.create(user=user, prize=prize_amount, is_approved=True)

    return JsonResponse({'success': True, 'prize': winning_prize_str, 'remaining_spins': user.roulette_spins})

@login_required
def sobre(request):
    user = request.user

    if request.method == 'POST':
        action = request.POST.get('action')

        # Atualizar a foto de perfil do usuário
        if action == 'update_profile_picture':
            if 'profile_picture' in request.FILES:
                user.profile_picture = request.FILES['profile_picture']
                user.save()
                messages.success(request, 'Foto de perfil atualizada com sucesso!')
            else:
                messages.error(request, 'Nenhuma imagem foi selecionada.')

        # Criar uma nova mensagem/postagem no grupo
        elif action == 'create_post':
            # Bloqueia envio se o usuário estiver restrito pelo admin
            if getattr(user, 'is_restricted', False):
                messages.error(request, 'Você está impedido de enviar mensagens neste grupo.')
                return redirect('sobre')

            content = request.POST.get('content', '').strip()
            image = request.FILES.get('image')

            if content or image:
                Post.objects.create(
                    user=user,
                    content=content,
                    image=image
                )
                messages.success(request, 'Mensagem enviada!')
            else:
                messages.error(request, 'Escreva uma mensagem ou selecione uma imagem para enviar.')

        return redirect('sobre')

    # Busca o texto institucional da plataforma
    platform_settings = PlatformSettings.objects.first()
    history_text = platform_settings.history_text if platform_settings else 'Informação indisponível.'

    # Lista de posts em ordem cronológica
    posts = Post.objects.select_related('user').all().order_by('created_at')

    context = {
        'history_text': history_text,
        'posts': posts,
    }
    return render(request, 'sobre.html', context)

@login_required
def perfil(request):
    bank_details, _ = BankDetails.objects.get_or_create(user=request.user)
    withdrawal_records = Withdrawal.objects.filter(user=request.user).order_by('-created_at')
    if request.method == 'POST':
        if 'update_bank' in request.POST:
            form = BankDetailsForm(request.POST, instance=bank_details)
            if form.is_valid():
                form.save()
                messages.success(request, 'Banco updated.')
        elif 'change_password' in request.POST:
            password_form = PasswordChangeForm(request.user, request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, 'Senha alterada.')
        return redirect('perfil')
    
    context = {
        'form': BankDetailsForm(instance=bank_details),
        'password_form': PasswordChangeForm(request.user),
        'user_levels': UserLevel.objects.filter(user=request.user, is_active=True),
        'withdrawal_records': withdrawal_records,
    }
    return render(request, 'perfil.html', context)

@login_required
def renda(request):
    user = request.user
    active_levels = UserLevel.objects.filter(user=user, is_active=True)
    approved_deposit_total = Deposit.objects.filter(user=user, is_approved=True).aggregate(Sum('amount'))['amount__sum'] or 0
    today = date.today()
    daily_income = Task.objects.filter(user=user, completed_at__date=today).aggregate(Sum('earnings'))['earnings__sum'] or 0
    total_withdrawals = Withdrawal.objects.filter(user=user, status='Aprovado').aggregate(Sum('amount'))['amount__sum'] or 0
    total_income = (Task.objects.filter(user=user).aggregate(Sum('earnings'))['earnings__sum'] or 0) + user.subsidy_balance
    
    context = {
        'user': user,
        'has_active_plans': active_levels.exists(),
        'approved_deposit_total': approved_deposit_total,
        'daily_income': daily_income,
        'total_withdrawals': total_withdrawals,
        'total_income': total_income,
    }
    return render(request, 'renda.html', context)
    