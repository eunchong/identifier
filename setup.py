try:
    from setuptools import setup
    from setuptools import find_packages
    packages = find_packages()
except ImportError:
    from distutils.core import setup
    import os
    packages = [x.strip('./').replace('/','.') for x in os.popen('find -name "__init__.py" | xargs -n1 dirname').read().strip().split('\n')]

setup(
    name='identifier',
    version='1.0',
    description='The function identifier',
    #packages=['identifier','identifier.functions'],
    #packages=['identifier'],
    packages=packages,
    include_package_data=True,
    install_requires=[
        'angr',
    ],
)
