# create_deployment_phase.py
"""
Создание полной Deployment фазы
"""

def create_deployment_phase():
    """Создает всех агентов deployment фазы"""
    
    agents = {
        'production-readiness-checker': {
            'role': 'Проверка готовности к production',
            'priority': 1,
            'blocking': True
        },
        'deployment-engineer': {
            'role': 'Деплой в production',
            'priority': 2,
            'depends_on': ['production-readiness-checker']
        },
        'marketing-strategist': {
            'role': 'Маркетинг и реклама',
            'priority': 2,
            'parallel_with': ['deployment-engineer']
        },
        'launch-coordinator': {
            'role': 'Координация запуска',
            'priority': 3,
            'depends_on': ['deployment-engineer', 'marketing-strategist']
        }
    }
    
    # Создать директории для каждого агента
    for agent_name, agent_config in agents.items():
        agent_dir = Path(f".openclaw/agents/deployment/{agent_name}")
        agent_dir.mkdir(parents=True, exist_ok=True)
        
        # Сохранить конфиг
        with open(agent_dir / "config.yaml", 'w') as f:
            yaml.dump(agent_config, f)
        
        print(f"✅ Created {agent_name}")
    
    # Обновить workflow config
    workflow_config = Path("workflow/config.yaml")
    with open(workflow_config, 'r') as f:
        config = yaml.safe_load(f)
    
    # Добавить deployment фазу
    config['phases']['deployment'] = {
        'description': 'Деплой и запуск продукта',
        'mode': 'sequential',
        'approval_required': True,
        'agents': [
            {
                'name': 'production-readiness-checker',
                'command': 'openclaw run production-readiness-checker',
                'timeout': 900,
                'blocking': True
            },
            {
                'parallel_group': [
                    {
                        'name': 'deployment-engineer',
                        'command': 'openclaw run deployment-engineer',
                        'timeout': 1200
                    },
                    {
                        'name': 'marketing-strategist',
                        'command': 'openclaw run marketing-strategist',
                        'timeout': 900
                    }
                ]
            },
            {
                'name': 'launch-coordinator',
                'command': 'openclaw run launch-coordinator',
                'timeout': 1800,
                'depends_on': ['deployment-engineer', 'marketing-strategist']
            }
        ]
    }
    
    with open(workflow_config, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    
    print("\n✅ Deployment фаза создана!")
    print("\n📋 Структура:")
    print("  1. production-readiness-checker (блокирующий)")
    print("  2. deployment-engineer + marketing-strategist (параллельно)")
    print("  3. launch-coordinator (финальная координация)")

if __name__ == '__main__':
    create_deployment_phase()