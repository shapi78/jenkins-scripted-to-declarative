pipeline {
    agent { label 'linux' }
    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }
        stage('Build') {
            steps {
                sh 'make build'
            }
        }
        stage('Test') {
            parallel {
                stage("unit") {
                    steps {
                    sh 'make test-unit'
                    }
                }
                stage("integration") {
                    steps {
                    sh 'make test-integration'
                    }
                }
            }
        }
        stage('Deploy') {
            steps {
                script {
                            def version = readFile('VERSION').trim()
                            if (env.BRANCH_NAME == 'main') {
                                sh "./deploy.sh ${version}"
                            }
                }
            }
        }
    }
}
