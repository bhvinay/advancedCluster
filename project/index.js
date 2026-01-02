#!/usr/bin/env node

const Wizard = require('./wizard');
const PrerequisiteChecker = require('./prerequisite-checker');

const args = process.argv.slice(2);

if (args.length === 0) {
    console.log('🚀 Advanced Cluster Setup Tool');
    console.log('Usage: project <command>');
    console.log('Run "project prereq" to check prerequisites first');
    process.exit(0);
}

const command = args[0];

switch (command) {
    case 'prereq':
        const checker = new PrerequisiteChecker();
        checker.runAll();
        break;
    case 'hello':
        console.log('Hello World!');
        break;
    case 'version':
        console.log('v1.0.0');
        break;
    case 'wizard':
        const setupWizard = new Wizard('Project Setup Wizard', [
            {
                key: 'name',
                title: 'Project Name',
                prompt: 'Enter your project name',
                validate: (input) => input.trim().length > 0,
                errorMessage: 'Project name cannot be empty'
            },
            {
                key: 'description',
                title: 'Project Description',
                prompt: 'Enter a brief description'
            },
            {
                key: 'author',
                title: 'Author Information',
                prompt: 'Enter your name'
            },
            {
                key: 'license',
                title: 'License',
                prompt: 'Choose license (MIT/Apache/GPL)',
                validate: (input) => ['MIT', 'Apache', 'GPL'].includes(input),
                errorMessage: 'Please choose MIT, Apache, or GPL'
            }
        ]);
        setupWizard.start();
        break;
    case 'help':
        console.log('Available commands:');
        console.log('  prereq  - Check prerequisites and learn about requirements');
        console.log('  wizard  - Run setup wizard');
        console.log('  hello   - Print hello message');
        console.log('  version - Show version');
        console.log('  help    - Show this help');
        break;
    default:
        console.log(`Unknown command: ${command}`);
        console.log('Run "project help" for available commands');
        process.exit(1);
}