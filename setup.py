#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OS_Agent 安装配置脚本
用于将OS_Agent打包并安装为Python包
Secondary development based on LinuxAgent original author Eilen6316

使用方法:
    1. 直接安装: python setup.py install
    2. 开发模式安装: python setup.py develop
    3. 构建分发包: python setup.py sdist bdist_wheel
    4. 上传到PyPI: python setup.py sdist bdist_wheel && twine upload dist/*
"""

from setuptools import setup, find_packages


with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as f:
    requirements = f.read().splitlines()

# 配置包的元数据和安装选项
setup(
    name="os_agent",        
    version="3.0.1",
    author="OS_Agent Team",
    author_email="",
    description="基于大语言模型的操作系统智能代理",  
    long_description=long_description,  
    long_description_content_type="text/markdown",  
    
    url="https://github.com/xiaoben765/OS_Agent",
    project_urls={
        "Original Project": "https://github.com/Eilen6316/LinuxAgent",
    },
    
    packages=find_packages(),     
    
    classifiers=[
        "Programming Language :: Python :: 3",  
        "License :: OSI Approved :: MIT License",  
        "Operating System :: OS Independent",  
    ],
    
    python_requires=">=3.7",     

    install_requires=requirements,  
    
    entry_points={
        "console_scripts": [
            "os_agent=os_agent:main",  
        ],
    },
) 
