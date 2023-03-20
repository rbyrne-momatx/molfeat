from setuptools import setup
from setuptools import find_packages

# Sync the env.yml file here
install_requires = [
    "tqdm",
    "loguru",
    "joblib",
    "fsspec",
    "pandas",
    "numpy",
    "scipy",
    "platformdirs",
]

setup(
    name="molfeat",
    version="0.6.1",
    author="Emmanuel Noutahi",
    author_email="emmanuel@valencediscovery.com",
    url="https://github.com/valence-platform/molfeat",
    description="A python library to featurize molecules.",
    long_description=open("README.md", encoding="utf8").read(),
    long_description_content_type="text/markdown",
    project_urls={
        "Bug Tracker": "https://github.com/valence-platform/molfeat/issues",
        "Source Code": "https://github.com/valence-platform/molfeat",
    },
    python_requires=">=3.7",
    install_requires=install_requires,
    packages=find_packages(),
    include_package_data=True,
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Intended Audience :: Healthcare Industry",
        "Intended Audience :: Science/Research",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Scientific/Engineering :: Bio-Informatics",
        "Topic :: Scientific/Engineering :: Information Analysis",
        "Topic :: Scientific/Engineering :: Medical Science Apps.",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
    ],
    entry_points={"console_scripts": []},
)
