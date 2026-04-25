import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from src.agent import Agent


class AgentDirectCommandTests(unittest.TestCase):
    def test_execute_direct_command_shows_stdout_via_show_result(self):
        agent = Agent.__new__(Agent)
        agent.ui = SimpleNamespace(
            console=Mock(),
            show_result=Mock(),
            show_error=Mock(),
            confirm=Mock(return_value=True),
        )
        agent.executor = SimpleNamespace(
            is_command_safe=Mock(return_value=(True, "")),
            execute_command=Mock(return_value=("hello world", "", 0)),
        )
        agent.config = SimpleNamespace(
            security=SimpleNamespace(confirm_dangerous_commands=True),
        )
        agent.logger = Mock()
        agent._record_intelligence_data = Mock()

        agent._execute_direct_command("echo hello", "测试直接命令执行")

        agent.ui.show_result.assert_called_once_with("hello world")
        agent.ui.show_error.assert_not_called()
        agent._record_intelligence_data.assert_called_once()

    def test_handle_shared_task_flow_passes_cli_credential_callback(self):
        agent = Agent.__new__(Agent)
        plan = SimpleNamespace()
        trace = SimpleNamespace()
        agent.task_orchestrator = SimpleNamespace(
            build_plan=Mock(return_value=plan),
            execute_plan=Mock(return_value=trace),
        )
        agent._present_task_plan = Mock()
        agent._confirm_task_step = Mock(return_value=True)
        agent._request_cli_credentials = Mock(return_value={"action": "submit", "password": "secret"})
        agent._apply_trace_stats = Mock()
        agent._show_task_trace = Mock()
        agent.logger = Mock()

        result = agent._handle_shared_task_flow("创建一个普通用户 demo_user，并验证是否创建成功")

        self.assertTrue(result)
        agent.task_orchestrator.execute_plan.assert_called_once()
        kwargs = agent.task_orchestrator.execute_plan.call_args.kwargs
        self.assertIs(kwargs["approval_callback"], agent._confirm_task_step)
        self.assertIs(kwargs["credential_callback"], agent._request_cli_credentials)

    def test_process_user_input_prefers_shared_task_flow_over_nlp_direct_execution(self):
        agent = Agent.__new__(Agent)
        agent.logger = Mock()
        agent.history = []
        agent.context_manager = None
        agent.system_monitor = None
        agent.chat_history = []
        agent.max_chat_history = 20
        agent.enable_recommendations = False
        agent.working_mode = "auto"
        agent.ui = SimpleNamespace(
            console=Mock(),
            confirm=Mock(return_value=True),
            show_thinking=Mock(),
        )
        agent.nlp_enhancer = SimpleNamespace(
            translate_to_command=Mock(
                return_value=SimpleNamespace(
                    confidence=0.95,
                    translated_command="echo direct-path",
                    explanation="基于模式匹配",
                )
            )
        )
        agent._handle_special_commands = Mock(return_value=False)
        agent._is_question_mode = Mock(return_value=False)
        agent._handle_question_mode = Mock()
        agent._handle_shared_task_flow = Mock(return_value=True)
        agent._execute_direct_command = Mock()
        agent.get_command_recommendations = Mock(return_value=[])
        agent.show_command_recommendations = Mock()
        agent._save_chat_history = Mock()

        agent.process_user_input("帮我查看当前系统中CPU占用最高的程序，并指出该程序的路径以及相关的权限")

        agent._handle_shared_task_flow.assert_called_once()
        agent._execute_direct_command.assert_not_called()

    def test_is_question_mode_does_not_route_imperative_user_creation_to_chat(self):
        agent = Agent.__new__(Agent)
        agent.logger = Mock()
        agent.config = SimpleNamespace(ui=SimpleNamespace(always_stream=True))

        result = agent._is_question_mode("创建一个普通用户 demo_user，并验证是否创建成功")

        self.assertFalse(result)

    def test_is_question_mode_does_not_route_cleanup_request_to_chat(self):
        agent = Agent.__new__(Agent)
        agent.logger = Mock()
        agent.config = SimpleNamespace(ui=SimpleNamespace(always_stream=True))

        result = agent._is_question_mode("帮我清理临时文件")

        self.assertFalse(result)

    def test_handle_shared_task_flow_continues_after_clarification_answer(self):
        agent = Agent.__new__(Agent)
        clarify_plan = SimpleNamespace(
            task_id="task-clarify-001",
            response_mode="clarify",
            clarification_prompt="请补充要创建的用户名。",
            original_input="创建一个普通用户",
            user_intent="创建普通用户",
        )
        execute_plan = SimpleNamespace(task_id="task-clarify-001", response_mode="execute")
        trace = SimpleNamespace(
            final_feedback="任务完成",
            steps=[],
            trace_summary={},
        )
        agent.task_orchestrator = SimpleNamespace(
            build_plan=Mock(side_effect=[clarify_plan, execute_plan]),
            execute_plan=Mock(return_value=trace),
        )
        agent.context_manager = SimpleNamespace(
            add_temporary_data=Mock(),
            get_temporary_data=Mock(side_effect=[
                None,
                None,
                {"task_id": "task-clarify-001", "original_input": "创建一个普通用户", "clarification_prompt": "请补充要创建的用户名。"},
                {"task_id": "task-clarify-001", "original_input": "创建一个普通用户", "clarification_prompt": "请补充要创建的用户名。"},
            ]),
            set_ongoing_task=Mock(),
            complete_task=Mock(),
            context_state=SimpleNamespace(temporary_data={}),
        )
        agent._present_task_plan = Mock()
        agent._confirm_task_step = Mock(return_value=True)
        agent._request_cli_credentials = Mock(return_value={"action": "submit", "password": "secret"})
        agent._apply_trace_stats = Mock()
        agent._show_task_trace = Mock()
        agent.logger = Mock()

        first_result = agent._handle_shared_task_flow("创建一个普通用户")
        second_result = agent._handle_shared_task_flow("demo_user")

        self.assertTrue(first_result)
        self.assertTrue(second_result)
        self.assertEqual(agent.task_orchestrator.build_plan.call_count, 2)
        second_input = agent.task_orchestrator.build_plan.call_args_list[1].args[0]
        self.assertIn("创建一个普通用户", second_input)
        self.assertIn("demo_user", second_input)
        self.assertEqual(agent.task_orchestrator.build_plan.call_args_list[1].kwargs["task_id"], "task-clarify-001")
        self.assertEqual(agent.task_orchestrator.execute_plan.call_count, 2)
        agent.task_orchestrator.execute_plan.assert_has_calls(
            [
                call(
                    clarify_plan,
                    approval_callback=agent._confirm_task_step,
                    event_callback=agent._on_shared_task_event,
                    credential_callback=agent._request_cli_credentials,
                ),
                call(
                    execute_plan,
                    approval_callback=agent._confirm_task_step,
                    event_callback=agent._on_shared_task_event,
                    credential_callback=agent._request_cli_credentials,
                ),
            ]
        )

    def test_handle_shared_task_flow_executes_clarify_plan_for_persistence(self):
        agent = Agent.__new__(Agent)
        clarify_plan = SimpleNamespace(
            task_id="task-clarify-002",
            response_mode="clarify",
            clarification_prompt="请补充要清理的具体配置目录。",
            original_input="删除 /etc 下面没用的配置",
            user_intent="删除或清理类 Linux 操作请求",
        )
        clarify_trace = SimpleNamespace(
            status="needs_clarification",
            final_feedback="请补充要清理的具体配置目录。",
            steps=[],
            trace_summary={},
        )
        agent.task_orchestrator = SimpleNamespace(
            build_plan=Mock(return_value=clarify_plan),
            execute_plan=Mock(return_value=clarify_trace),
        )
        agent.context_manager = None
        agent._present_task_plan = Mock()
        agent._store_pending_clarification = Mock()
        agent._confirm_task_step = Mock(return_value=True)
        agent._request_cli_credentials = Mock(return_value={"action": "submit", "password": "secret"})
        agent._apply_trace_stats = Mock()
        agent._show_task_trace = Mock()
        agent.logger = Mock()

        result = agent._handle_shared_task_flow("删除 /etc 下面没用的配置")

        self.assertTrue(result)
        agent._store_pending_clarification.assert_called_once_with(clarify_plan)
        agent.task_orchestrator.execute_plan.assert_called_once_with(
            clarify_plan,
            approval_callback=agent._confirm_task_step,
            event_callback=agent._on_shared_task_event,
            credential_callback=agent._request_cli_credentials,
        )
        agent._apply_trace_stats.assert_called_once_with(clarify_trace, "删除 /etc 下面没用的配置")
        agent._show_task_trace.assert_called_once_with(clarify_trace)

    def test_request_cli_credentials_uses_isolated_password_prompt(self):
        agent = Agent.__new__(Agent)
        agent.ui = SimpleNamespace(
            console=Mock(),
            session=SimpleNamespace(prompt=Mock(side_effect=AssertionError("shared session should not be used for passwords"))),
            get_input=Mock(side_effect=AssertionError("plain input fallback should not be used when isolated password prompt works")),
            show_error=Mock(),
        )

        with patch("getpass.getpass", return_value="secret") as mocked_getpass:
            result = agent._request_cli_credentials("请输入 sudo 密码", SimpleNamespace(index=1, command="sudo ss -tunap"), 1, "")

        self.assertEqual(result, {"action": "submit", "password": "secret"})
        mocked_getpass.assert_called_once()
        agent.ui.show_error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
