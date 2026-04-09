from pathlib import Path

from setuptools import find_packages, setup


setup(
    name="solo-agent-shinobi",
    version="0.1.0",
    description="Single-agent GitHub issue workflow automation.",
    long_description=Path("README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="solo-agent-shinobi maintainers",
    python_requires=">=3.9",
    package_dir={"": "src"},
    packages=find_packages("src"),
    include_package_data=True,
    package_data={
        "shinobi": ["bootstrap_templates/*.md"],
        "shinobi.bootstrap_templates": ["*.md"],
    },
    entry_points={"console_scripts": ["shinobi=shinobi.cli:main"]},
)
