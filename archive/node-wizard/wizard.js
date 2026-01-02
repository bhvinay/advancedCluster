const readlineSync = require('readline-sync');

class Wizard {
    constructor(name, steps) {
        this.name = name;
        this.steps = steps;
        this.currentStep = 0;
        this.answers = {};
    }

    start() {
        console.log(`\n=== ${this.name} ===\n`);
        this.runStep();
    }

    runStep() {
        if (this.currentStep >= this.steps.length) {
            this.complete();
            return;
        }

        const step = this.steps[this.currentStep];
        console.log(`Step ${this.currentStep + 1}/${this.steps.length}: ${step.title}`);
        
        if (step.description) {
            console.log(step.description);
        }

        const answer = readlineSync.question(`${step.prompt}: `);
        
        if (step.validate && !step.validate(answer)) {
            console.log(step.errorMessage || 'Invalid input. Please try again.');
            return this.runStep();
        }

        this.answers[step.key] = answer;
        this.currentStep++;
        
        console.log();
        this.runStep();
    }

    complete() {
        console.log('=== Wizard Complete ===');
        console.log('Your answers:');
        Object.entries(this.answers).forEach(([key, value]) => {
            console.log(`  ${key}: ${value}`);
        });
        console.log();
    }
}

module.exports = Wizard;