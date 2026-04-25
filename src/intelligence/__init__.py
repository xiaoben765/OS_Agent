#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
智能化功能模块
提供命令学习、智能推荐、知识库和自然语言增强等功能
"""

from .command_learner import CommandLearner
from .recommendation_engine import RecommendationEngine
from .knowledge_base import KnowledgeBase
from .nlp_enhancer import NLPEnhancer
from .pattern_analyzer import PatternAnalyzer
from .context_manager import ContextManager

__all__ = [
    'CommandLearner',
    'RecommendationEngine', 
    'KnowledgeBase',
    'NLPEnhancer',
    'PatternAnalyzer',
    'ContextManager'
]

__version__ = "2.1.1"
__author__ = "OS_Agent Team"
__credits__ = "LinuxAgent original author: Eilen6316 - https://github.com/Eilen6316/LinuxAgent"
