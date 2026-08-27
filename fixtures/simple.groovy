node('linux') {
    stage('Checkout') {
        checkout scm
    }
    stage('Build') {
        sh 'make build'
    }
    stage('Test') {
        parallel(
            unit: {
                sh 'make test-unit'
            },
            integration: {
                sh 'make test-integration'
            }
        )
    }
    stage('Deploy') {
        def version = readFile('VERSION').trim()
        if (env.BRANCH_NAME == 'main') {
            sh "./deploy.sh ${version}"
        }
    }
}
