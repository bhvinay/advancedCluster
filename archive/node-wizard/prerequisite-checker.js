const readlineSync = require('readline-sync');

class PrerequisiteChecker {
    constructor() {
        this.checks = [
            {
                id: 'org_privileges',
                title: 'Organization Privileges',
                description: 'Verify that you have the correct privileges in your organization',
                check: this.checkOrgPrivileges.bind(this)
            },
            {
                id: 'aws_subscriptions',
                title: 'AWS Subscriptions',
                description: 'Verify that you have the necessary AWS subscriptions',
                check: this.checkAwsSubscriptions.bind(this)
            },
            {
                id: 'roles_policies',
                title: 'Roles and Policies',
                description: 'Learn about the roles and policies in your environment',
                check: this.learnRolesPolicies.bind(this)
            },
            {
                id: 'secure_agent',
                title: 'Secure Agent & Cluster Access',
                description: 'Learn how the Secure Agent and advanced cluster access resources on your cloud platform',
                check: this.learnSecureAgent.bind(this)
            },
            {
                id: 'packages_images',
                title: 'Packages and Images',
                description: 'Learn about the packages and images that the advanced cluster uses',
                check: this.learnPackagesImages.bind(this)
            }
        ];
        this.results = {};
    }

    async runAll() {
        console.log('\n=== Pre-requisite Checker ===\n');
        console.log('This tool will help verify your setup and provide learning resources.\n');

        for (let i = 0; i < this.checks.length; i++) {
            const check = this.checks[i];
            console.log(`\n[${i + 1}/${this.checks.length}] ${check.title}`);
            console.log(`${check.description}\n`);
            
            const result = await check.check();
            this.results[check.id] = result;
            
            if (i < this.checks.length - 1) {
                const continueCheck = readlineSync.keyInYNStrict('Continue to next check?');
                if (!continueCheck) {
                    console.log('Pre-requisite check stopped by user.');
                    return false;
                }
            }
        }

        this.showSummary();
        return true;
    }

    checkOrgPrivileges() {
        console.log('Organization Privileges Verification:');
        console.log('• Admin access to your organization');
        console.log('• Permission to create and manage cloud resources');
        console.log('• Access to billing and subscription management');
        
        const hasAdmin = readlineSync.keyInYNStrict('Do you have admin access to your organization?');
        const hasCloudPerms = readlineSync.keyInYNStrict('Do you have permission to create cloud resources?');
        const hasBilling = readlineSync.keyInYNStrict('Do you have access to billing management?');
        
        const allValid = hasAdmin && hasCloudPerms && hasBilling;
        
        if (!allValid) {
            console.log('\n⚠️  Warning: Missing some required privileges. Contact your administrator.');
        } else {
            console.log('\n✅ Organization privileges verified!');
        }
        
        return { status: allValid ? 'passed' : 'warning', details: { hasAdmin, hasCloudPerms, hasBilling } };
    }

    checkAwsSubscriptions() {
        console.log('AWS Subscriptions Verification:');
        console.log('Required AWS services:');
        console.log('• Amazon EKS (Elastic Kubernetes Service)');
        console.log('• Amazon EC2 (Elastic Compute Cloud)');
        console.log('• Amazon VPC (Virtual Private Cloud)');
        console.log('• AWS IAM (Identity and Access Management)');
        console.log('• Amazon ECR (Elastic Container Registry)');
        
        const hasAccount = readlineSync.keyInYNStrict('Do you have an active AWS account?');
        if (!hasAccount) {
            console.log('\n❌ AWS account required. Please create one at https://aws.amazon.com/');
            return { status: 'failed', details: { hasAccount: false } };
        }
        
        const hasPermissions = readlineSync.keyInYNStrict('Do you have permissions to use EKS, EC2, VPC, IAM, and ECR?');
        if (!hasPermissions) {
            console.log('\n⚠️  Warning: You may need additional AWS permissions.');
        } else {
            console.log('\n✅ AWS subscriptions verified!');
        }
        
        return { status: hasPermissions ? 'passed' : 'warning', details: { hasAccount, hasPermissions } };
    }

    learnRolesPolicies() {
        console.log('Roles and Policies Learning Module:');
        console.log('\nKey AWS IAM concepts:');
        console.log('• IAM Roles: Define what actions can be performed');
        console.log('• IAM Policies: JSON documents that define permissions');
        console.log('• Service Roles: Roles that AWS services can assume');
        console.log('• Instance Profiles: Container for IAM roles for EC2');
        
        console.log('\nRequired roles for advanced cluster:');
        console.log('• EKS Cluster Service Role');
        console.log('• EKS Node Group Role');
        console.log('• AWS Load Balancer Controller Role');
        console.log('• EBS CSI Driver Role');
        
        const wantsMore = readlineSync.keyInYNStrict('Would you like to see example policies?');
        if (wantsMore) {
            console.log('\nExample EKS Cluster Service Role policy:');
            console.log('• AmazonEKSClusterPolicy');
            console.log('• AmazonEKSVPCResourceController (for VPC resource management)');
        }
        
        console.log('\n📚 Learning completed!');
        return { status: 'completed', details: { viewedExamples: wantsMore } };
    }

    learnSecureAgent() {
        console.log('Secure Agent & Advanced Cluster Access Learning:');
        console.log('\nSecure Agent Overview:');
        console.log('• Lightweight connector between your network and cloud services');
        console.log('• Enables secure communication without opening firewall ports');
        console.log('• Runs as a service in your environment');
        
        console.log('\nAdvanced Cluster Access:');
        console.log('• Kubernetes clusters access cloud resources via IAM roles');
        console.log('• Uses OIDC (OpenID Connect) for secure authentication');
        console.log('• Service accounts map to IAM roles via annotations');
        console.log('• Enables least-privilege access patterns');
        
        console.log('\nCloud Platform Integration:');
        console.log('• EKS integrates with AWS services (ECR, ELB, EBS)');
        console.log('• Pods can access AWS APIs using IAM roles');
        console.log('• Network policies control inter-pod communication');
        
        const currentPlatform = readlineSync.question('What cloud platform are you using? (AWS/Azure/GCP): ');
        
        console.log('\n🔒 Security concepts learned!');
        return { status: 'completed', details: { platform: currentPlatform } };
    }

    learnPackagesImages() {
        console.log('Packages and Images Learning Module:');
        console.log('\nContainer Images used by advanced cluster:');
        console.log('• Kubernetes system images (kube-proxy, coredns, etc.)');
        console.log('• AWS Load Balancer Controller');
        console.log('• EBS CSI Driver');
        console.log('• Cluster Autoscaler');
        console.log('• Metrics Server');
        
        console.log('\nPackage Management:');
        console.log('• Helm charts for application deployment');
        console.log('• Kustomize for configuration management');
        console.log('• ECR for private container registry');
        
        console.log('\nImage Security:');
        console.log('• Image vulnerability scanning');
        console.log('• Image signing and verification');
        console.log('• Registry access controls');
        
        const hasRegistry = readlineSync.keyInYNStrict('Do you have access to a container registry (ECR/DockerHub)?');
        const knowsHelm = readlineSync.keyInYNStrict('Are you familiar with Helm package manager?');
        
        if (!hasRegistry) {
            console.log('\n⚠️  Consider setting up Amazon ECR for private images.');
        }
        if (!knowsHelm) {
            console.log('\n📖 Recommend learning Helm: https://helm.sh/docs/');
        }
        
        console.log('\n📦 Package and image concepts learned!');
        return { status: 'completed', details: { hasRegistry, knowsHelm } };
    }

    showSummary() {
        console.log('\n=== Pre-requisite Check Summary ===\n');
        
        let passed = 0;
        let warnings = 0;
        let failed = 0;
        
        this.checks.forEach(check => {
            const result = this.results[check.id];
            let status = '❓';
            
            if (result.status === 'passed') {
                status = '✅';
                passed++;
            } else if (result.status === 'warning') {
                status = '⚠️ ';
                warnings++;
            } else if (result.status === 'failed') {
                status = '❌';
                failed++;
            } else if (result.status === 'completed') {
                status = '📚';
                passed++;
            }
            
            console.log(`${status} ${check.title}`);
        });
        
        console.log(`\nSummary: ${passed} passed, ${warnings} warnings, ${failed} failed`);
        
        if (failed > 0) {
            console.log('\n❌ Please address failed checks before proceeding.');
            return false;
        } else if (warnings > 0) {
            console.log('\n⚠️  Some warnings detected. Proceed with caution.');
        } else {
            console.log('\n🎉 All pre-requisites satisfied!');
        }
        
        return true;
    }
}

module.exports = PrerequisiteChecker;